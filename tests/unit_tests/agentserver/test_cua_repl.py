"""Programmatic CUA integration: real MCP execution and lifecycle boundaries."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from mcp import StdioServerParameters, types
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.foundation.llm import AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.subagents.cua_agent import CUA_AGENT_FACTORY_NAME, create_cua_agent
from openjiuwen.harness.rails.multimodal_context_summarizer_rail import MultimodalContextSummarizerRail
from openjiuwen.harness.tools.cua.rails import (
    CuaProgressRail, CuaRepeatFailureRail, CuaRuntimeRail, CuaScreenshotDownscaleRail,
)
from openjiuwen.harness.tools.cua.cua_capabilities import (
    APP_LIFECYCLE_CUA_TOOL_NAMES, CORE_CUA_TOOL_NAMES, INPUT_CUA_TOOL_NAMES,
)
import pytest
import psutil
from PIL import Image

from jiuwenswarm.agents.harness.computer_use.config import build_cua_agent_config
from jiuwenswarm.agents.harness.common.cua_defaults import build_cua_agent_card, resolve_cua_factory_options
from jiuwenswarm.agents.harness.computer_use.prompt import build_system_prompt, engine_root
from jiuwenswarm.agents.harness.computer_use.rail import CuaExtensionRail
from jiuwenswarm.agents.harness.computer_use.runtime import ReplRuntime, desktop_lane
from jiuwenswarm.agents.harness.computer_use.tools import CuaReplTool, NativeTool, convert_result
from jiuwenswarm.agents.harness.computer_use.worker import prepare_bundle


def _content(result):
    return "\n".join(part.text for part in result.content if part.type == "text")


def _runtime():
    # Run the real bundled MCP and Python supervisor, without starting a
    # desktop driver: these programs exercise namespace/lifetime semantics.
    source = str(engine_root() / "src")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")])))
    params = StdioServerParameters(command=sys.executable, args=["-m", "computer_use.mcp"], env=env)
    return ReplRuntime(parameters=params, startup_timeout=20, call_timeout=20)


@pytest.fixture
async def materialize(monkeypatch):
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", MagicMock())
    await Runner.start()
    owned = []

    async def make(rail=None, keep=3, card=None):
        rail = rail or CuaExtensionRail()
        agent = create_cua_agent(
            MagicMock(), card=card, rails=[rail], auto_create_workspace=False,
            enable_sys_operation=False, enable_skill_discovery=False,
            enable_read_image_multimodal=True, cua_snapshot_keep_last_k=keep,
        )
        owned.append(rail)
        await agent._ensure_initialized()
        return agent, rail

    yield make
    for rail in owned:
        await rail.close()
    await Runner.stop()


async def test_default_child_keeps_all_native_tools_and_adds_one_repl(materialize):
    spec = build_cua_agent_config(MagicMock(), card=build_cua_agent_card())
    agent, rail = await materialize(spec.rails[0].fork_for_agent())
    names = {tool.card.name for tool in rail.tools}
    expected = set(CORE_CUA_TOOL_NAMES + INPUT_CUA_TOOL_NAMES + APP_LIFECYCLE_CUA_TOOL_NAMES)
    assert names == expected | {"cua_repl"}
    assert "computer_use_reset" not in names
    assert spec.parallel_tool_calls is False
    assert spec.factory_name == CUA_AGENT_FACTORY_NAME
    for rail_type in (CuaRuntimeRail, CuaProgressRail, CuaRepeatFailureRail, CuaScreenshotDownscaleRail):
        assert len(agent.find_rails_by_type((rail_type,))) == 1
    for tool in rail.tools:
        assert Runner.resource_mgr.get_tool(tool_id=tool.card.id) is tool
    assert all(tool.original._desktop is rail.desktop for tool in rail.tools if isinstance(tool, NativeTool))


async def test_materialized_children_do_not_share_tools_or_runtime(materialize):
    template = CuaExtensionRail()
    card = build_cua_agent_card()
    (_, first), (_, second) = await asyncio.gather(
        materialize(template.fork_for_agent(), card=card),
        materialize(template.fork_for_agent(), card=card),
    )
    assert first.runtime is not second.runtime
    assert first.desktop is not second.desktop
    assert {t.card.id for t in first.tools}.isdisjoint(t.card.id for t in second.tools)
    for rail in (first, second):
        assert all(tool.original._desktop is rail.desktop for tool in rail.tools if isinstance(tool, NativeTool))
        assert all(Runner.resource_mgr.get_tool(tool_id=tool.card.id) is tool for tool in rail.tools)


async def test_native_backend_is_not_preflighted_during_construction(materialize):
    _, rail = await materialize()
    assert rail.desktop._backend is None
    backend = MagicMock()
    rail.desktop._backend_factory = backend
    ctx = AgentCallbackContext(agent=MagicMock())
    await rail.native_rail.before_invoke(ctx)
    backend.assert_called_once_with()
    backend.return_value.screen_size.assert_called_once_with()


async def test_capture_retention_is_forked_per_child(materialize):
    spec = build_cua_agent_config(MagicMock(), card=build_cua_agent_card(), cua_snapshot_keep_last_k=1)
    for rail in spec.rails:
        assert rail.fork_for_agent() is not rail
    assert spec.factory_kwargs["cua_snapshot_keep_last_k"] == 1
    first, _ = await materialize(spec.rails[0].fork_for_agent(), keep=1)
    second, _ = await materialize(spec.rails[0].fork_for_agent(), keep=1)
    a, = first.find_rails_by_type((MultimodalContextSummarizerRail,))
    b, = second.find_rails_by_type((MultimodalContextSummarizerRail,))
    assert a is not b and a._screenshots_to_keep == b._screenshots_to_keep == 1


async def test_disabled_repl_keeps_original_tools(materialize):
    _, rail = await materialize(CuaExtensionRail(repl_enabled=False))
    assert len(rail.tools) == 17
    assert "cua_repl" not in build_system_prompt("en", repl_enabled=False)


@pytest.mark.parametrize("language", ["cn", "en"])
def test_prompt_routes_workflows_and_adapts_the_skill(language):
    text = build_system_prompt(language)
    assert "cua_repl({" in text
    assert "class Computer:" in text and "class Element:" in text
    assert "computer_use({" not in text
    assert "computer_use_reset" not in text
    assert "simple" in text.lower() or "简单" in text
    assert "multi-step" in text or "多步" in text
    assert "not in a security sandbox" in text
    assert "Never reuse old-tool" in text


def test_repl_config_reaches_single_and_team_paths():
    assert resolve_cua_factory_options({"repl_enabled": False, "repl_python": " python3.12 "}) == {
        "cua_repl_enabled": False, "cua_repl_python": "python3.12",
    }
    assert resolve_cua_factory_options({"repl_enabled": "true", "repl_python": []}) == {}


@pytest.mark.parametrize("platform, heading", [("darwin", "macOS environment"), ("win32", "Windows environment")])
def test_prompt_uses_the_target_platform_skill(monkeypatch, platform, heading):
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.prompt.sys", SimpleNamespace(platform=platform))
    prompt = build_system_prompt("en")
    assert heading in prompt
    assert "computer_use_reset" not in prompt
    assert "description: Operate" not in prompt


def test_result_conversion_preserves_images_text_errors_and_metadata():
    png = base64.b64encode(b"original-png-bytes").decode()
    incoming = types.CallToolResult(
        content=[types.TextContent(type="text", text="Action [dispatched]; effect unknown"),
                 types.ImageContent(type="image", data=png, mimeType="image/png")],
        isError=True, structuredContent={"effect": "possible"}, _meta={"executionId": 9},
    )
    result = convert_result(incoming)
    assert result.success is False
    assert "effect unknown" in result.data["content"]
    assert result.data["multimodal"][0]["data_url"].endswith(png)
    assert result.data["mcp_meta"] == {"executionId": 9}
    assert result.data["structured_content"] == {"effect": "possible"}


def test_text_only_mcp_failure_keeps_the_error_for_core_progress():
    result = convert_result(types.CallToolResult(
        content=[types.TextContent(type="text", text="stale_target; effect none")], isError=True))
    assert not result.success
    assert result.error == "stale_target; effect none"


@pytest.mark.parametrize("arguments", [{}, {"code": " "}, {"code": "print(1)", "session": "other"},
                                       {"code": "print(1)", "title": None}])
async def test_invalid_input_never_starts_the_worker(arguments):
    runtime = SimpleNamespace(execute=AsyncMock())
    result = await CuaReplTool(runtime, "test").invoke(arguments)
    assert not result.success
    runtime.execute.assert_not_awaited()


async def test_identical_failed_code_is_not_replayed_indefinitely():
    runtime = SimpleNamespace(execute=AsyncMock(return_value=types.CallToolResult(
        content=[types.TextContent(type="text", text="stale_target; effect possible")], isError=True)))
    tool = CuaReplTool(runtime, "test")
    for _ in range(4):
        result = await tool.invoke({"code": "await app.click(1,2)"})
    assert not result.success
    assert runtime.execute.await_count == 3
    assert "no code was replayed" in result.error


@pytest.mark.skipif(sys.version_info < (3, 12), reason="The bundled engine requires Python 3.12+")
async def test_real_mcp_preserves_python_state_and_isolates_connections():
    first, second = _runtime(), _runtime()
    try:
        await first.execute("counter = 41", " ")
        result = await first.execute("counter += 1\nprint(counter)")
        assert not result.isError and "42" in _content(result)
        other = await second.execute("print(counter)")
        assert other.isError and "NameError" in _content(other)
        result = await first.execute("print(counter)")
        assert not result.isError and "42" in _content(result)
    finally:
        await first.close()
        await second.close()
    assert first.owner.done() and second.owner.done()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="The bundled engine requires Python 3.12+")
async def test_cancelled_real_script_cleans_up_before_another_call(tmp_path):
    runtime = _runtime()
    await runtime.execute("counter = 1")
    marker = tmp_path / "started"
    pending = asyncio.create_task(runtime.execute(
        "import asyncio, os\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\nawait asyncio.sleep(60)\nprint('late')"))
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
    process = psutil.Process(int(marker.read_text()))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert runtime.owner.done() and runtime.client is None
    assert not process.is_running()
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.execute("print('must not run')")
    await runtime.close()


@pytest.mark.parametrize("tools_missing", [False, True])
async def test_failed_startup_or_cleanup_cannot_silently_reuse_the_desktop(monkeypatch, tools_missing):
    class FakeClient:
        async def initialize(self):
            pass

        async def list_tools(self):
            names = [] if tools_missing else ["computer_use", "computer_use_reset"]
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        async def call_tool(self, name, arguments):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="native cleanup failed")],
                isError=name == "computer_use_reset",
            )

    @asynccontextmanager
    async def fake_stdio(parameters):
        yield None, None

    @asynccontextmanager
    async def fake_session(*args, **kwargs):
        yield FakeClient()

    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.runtime.stdio_client", fake_stdio)
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.runtime.ClientSession", fake_session)
    runtime = ReplRuntime()
    if tools_missing:
        with pytest.raises(RuntimeError, match="cleanup tools"):
            await runtime.open()
        assert runtime.owner.done() and not runtime.used
        assert desktop_lane().blocker is None
    else:
        await runtime.execute("print(1)")
        try:
            with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
                await runtime.close()
            async with desktop_lane().acquire():
                pytest.fail("Unconfirmed cleanup admitted another action")
        except RuntimeError as error:
            assert "cleanup is unconfirmed" in str(error)
        finally:
            desktop_lane().blocker = None


async def test_old_and_new_tools_share_a_desktop_lane_and_native_cancel_waits():
    started, release = asyncio.Event(), asyncio.Event()
    async def old_call(*args, **kwargs):
        started.set()
        await release.wait()
        return ToolOutput(success=True, data={"content": "done"})
    original = SimpleNamespace(card=ToolCard(id="old", name="click", description="click"), invoke=old_call)
    old = NativeTool(original, "owner", AsyncMock())
    runtime = SimpleNamespace(execute=AsyncMock(return_value=types.CallToolResult(content=[])))
    repl = CuaReplTool(runtime, "owner")
    first = asyncio.create_task(old.invoke({}))
    await started.wait()
    second = asyncio.create_task(repl.invoke({"code": "print(1)"}))
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    runtime.execute.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).success
    assert desktop_lane().blocker is None


async def test_repl_error_reaches_delegation_progress(materialize):
    agent, rail = await materialize()
    ctx = AgentCallbackContext(agent=agent)
    await rail.progress.before_invoke(ctx)
    await rail.before_invoke(ctx)
    ctx.inputs = ToolCallInputs(tool_name="cua_repl", tool_result=ToolOutput(success=False, error="execution_unknown"))
    await rail.after_tool_call(ctx)
    ctx.inputs = SimpleNamespace(result={})
    await rail.progress.after_invoke(ctx)
    assert ctx.inputs.result["cua_result"]["status"] == "partial"
    assert any("execution_unknown" in item for item in ctx.inputs.result["cua_result"]["blockers"])


async def test_session_binding_uses_the_public_session_contract(materialize):
    _, rail = await materialize()
    first = rail.runtime
    ctx = AgentCallbackContext(agent=MagicMock(), session=Session(session_id="first"))
    await rail.before_invoke(ctx)
    assert rail.session_id == "first"
    assert rail.runtime is first
    ctx.session = Session(session_id="second")
    await rail.before_invoke(ctx)
    assert first.closing and rail.runtime is not first
    assert rail.session_id == "second"
    repl = next(tool for tool in rail.tools if tool.card.name == "cua_repl")
    assert repl.runtime is rail.runtime
    await rail.close()
    closed = rail.runtime
    await rail.before_invoke(ctx)
    assert rail.runtime is not closed


@pytest.mark.skipif(sys.version_info < (3, 12), reason="The bundled engine requires Python 3.12+")
async def test_task_tool_delegates_two_repl_calls_and_cleans_up(monkeypatch, tmp_path):
    """Real DeepAgent/TaskTool/stdio execution; only model replies are scripted."""
    class ScriptedModel:
        def __init__(self, replies):
            self.replies, self.seen = replies, []
            self.model_client_config = ModelClientConfig(
                client_provider="OpenAI", api_key="test", api_base="http://127.0.0.1:9")
            self.model_config = ModelRequestConfig(model="test-model")

        async def invoke(self, messages, **kwargs):
            self.seen.append(messages)
            assert self.replies, "Unexpected model request"
            return self.replies.pop(0)

        async def stream(self, messages, **kwargs):
            reply = await self.invoke(messages, **kwargs)
            yield AssistantMessageChunk(content=reply.content, tool_calls=reply.tool_calls)

    def call(name, arguments, identifier):
        return AssistantMessage(content="", tool_calls=[ToolCall(
            id=identifier, type="function", name=name, arguments=json.dumps(arguments))])

    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    encoded = base64.b64encode(image.getvalue()).decode()
    first_code = "value = 41\nprint(value)\nimport base64\n" + f"printImage(base64.b64decode({encoded!r}))"
    child_model = ScriptedModel([
        call("cua_repl", {"code": first_code}, "first"),
        call("cua_repl", {"code": "value += 1\nprint(value)"}, "second"),
        AssistantMessage(content="Verified 42"),
    ])
    parent_model = ScriptedModel([
        call("task_tool", {"subagent_type": "cua_agent", "task_description": "Verify two Python calls."}, "delegate"),
        AssistantMessage(content="Child verified 42"),
    ])
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", MagicMock())
    monkeypatch.setattr(CuaRuntimeRail, "before_invoke", AsyncMock())
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.rail.ReplRuntime", lambda python=None: _runtime())
    children = []
    original_init = CuaExtensionRail.init

    def record_child(rail, agent):
        original_init(rail, agent)
        children.append(rail)

    monkeypatch.setattr(CuaExtensionRail, "init", record_child)
    spec = build_cua_agent_config(child_model, card=build_cua_agent_card(), max_iterations=5)
    spec.factory_kwargs.update(auto_create_workspace=False, enable_sys_operation=False, enable_skill_discovery=False)
    await Runner.start()
    try:
        parent = create_deep_agent(
            model=parent_model, subagents=[spec], workspace=str(tmp_path), auto_create_workspace=False,
            enable_sys_operation=False, enable_skill_discovery=False, enable_read_image_multimodal=True,
            max_iterations=4,
        )
        result = await asyncio.wait_for(Runner.run_agent(parent, {"query": "Run the child verification."}), 30)
        assert result["output"] == "Child verified 42"
        assert len(parent_model.seen) == 2 and len(child_model.seen) == 3
        assert "data:image/png;base64," + encoded in str(child_model.seen[1])
        assert "42" in str(parent_model.seen[-1])
        assert len(children) == 1 and children[0].session_id
        runtime = children[0].runtime
        assert runtime.used and runtime.closing and runtime.owner.done()
        assert runtime.client is None and runtime.cleanup_error is None
    finally:
        for child in children:
            await child.close()
        await Runner.stop()


def test_bundle_changes_with_sources_and_ignores_build_outputs(tmp_path):
    source = tmp_path / "engine"
    for name in ("pyproject.toml", "backends/macos/Package.swift", "src/computer_use/main.py"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial", encoding="utf-8")
    cache = tmp_path / "cache"
    first = prepare_bundle(source, cache)
    built = source / "backends/macos/Sources/.build/ignored.o"
    built.parent.mkdir(parents=True, exist_ok=True)
    built.write_bytes(b"generated")
    assert prepare_bundle(source, cache) == first
    (source / "src/computer_use/main.py").write_text("updated", encoding="utf-8")
    assert prepare_bundle(source, cache) != first
    assert (first / "src/computer_use/main.py").read_text() == "initial"


def test_worker_import_keeps_protocol_stdout_clean():
    result = subprocess.run(
        [sys.executable, "-c", "import jiuwenswarm.agents.harness.computer_use.worker\nprint('protocol-ready')"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    assert result.stdout == "protocol-ready\n"
