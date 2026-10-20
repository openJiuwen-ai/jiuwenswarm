# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Permission rail initialization, isolated native callbacks and cancellation."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import test_flash_permission_actions as actions
from flash_sdk import sdk_tools
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.security.models import PermissionConfirmResponse

from jiuwenswarm.agents.harness.flash.permissions.flash_permission_rail import (
    FlashPermissionRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _RailBuildInfo

permission_adapter = actions.permission_adapter
tool_context = actions.tool_context


def writing_tool(path, writes):
    async def write(**kwargs):
        target = Path(kwargs.get("file_path", path))
        writes.append(str(target))
        target.write_text("fixture")
        return str(target)

    return LocalFunction(
        card=ToolCard(
            name="write_file",
            id="write_file",
            description="Temporary file write",
            input_params={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
        ),
        func=write,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission,approved,expected",
    [
        ("allow", True, 1),
        ("deny", True, 0),
        ("ask", False, 0),
        ("ask", True, 1),
    ],
)
async def test_write_allow_deny_and_hosted_decision(
    permission_adapter, tmp_path, permission, approved, expected
):
    _, rail = permission_adapter({"write_file": permission})
    rail._host.request_permission_confirmation = AsyncMock(
        return_value=PermissionConfirmResponse(approved=approved)
    )
    writes = []
    target = tmp_path / "result.txt"
    tool = writing_tool(target, writes)
    ctx = tool_context("write_file", {"file_path": str(target)})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert len(writes) == expected
        assert target.exists() == bool(expected)


@pytest.mark.asyncio
async def test_waiting_a_does_not_block_b_and_cancel_has_no_write(
    permission_adapter, tmp_path
):
    _, rail_a = permission_adapter({"write_file": "ask"})
    _, rail_b = permission_adapter({"write_file": "allow"})
    entered = asyncio.Event()

    async def wait_for_answer(_):
        entered.set()
        await asyncio.Event().wait()

    rail_a._host.request_permission_confirmation = wait_for_answer
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    writes_a, writes_b = [], []
    ctx_a = tool_context("write_file", {"file_path": str(a)}, session_id="a")
    ctx_b = tool_context("write_file", {"file_path": str(b)}, session_id="b")
    async with (
        sdk_tools(rail_a, ctx_a.session, [writing_tool(a, writes_a)]) as (
            ma,
            ca,
        ),
        sdk_tools(rail_b, ctx_b.session, [writing_tool(b, writes_b)]) as (
            mb,
            cb,
        ),
    ):
        pending = asyncio.create_task(
            ma.execute(ca, ctx_a.inputs.tool_call, ctx_a.session)
        )
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(mb.execute(cb, ctx_b.inputs.tool_call, ctx_b.session), 5)
        assert b.read_text() == "fixture"
        assert not a.exists()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert writes_a == [] and writes_b == [str(b)]
        # New A request after cancellation can execute normally.
        rail_a._host.request_permission_confirmation = AsyncMock(
            return_value=PermissionConfirmResponse(approved=True)
        )
        await ma.execute(
            ca,
            ctx_a.inputs.tool_call.model_copy(update={"id": "next-a"}),
            ctx_a.session,
        )
        assert writes_a == [str(a)]


@pytest.mark.asyncio
async def test_cold_warm_hot_update_keeps_actual_permission_instance(
    permission_adapter,
):
    adapter, _ = permission_adapter({"web_flash": "allow", "web_search": "deny"})
    rails = adapter._instantiate_rails(
        [
            _RailBuildInfo(
                "_permission_rail",
                adapter._build_permission_rail_for_agent,
                params={"config": {}},
            ),
        ],
        {},
    )
    rail = adapter._permission_rail
    assert isinstance(rail, FlashPermissionRail) and rail in rails
    identity = id(rail)
    for iteration in range(2):
        adapter._update_permission_rail({}, session_id="a")
        assert id(adapter._permission_rail) == identity
        ctx = tool_context(
            "web_flash", {"search": {"query": "fixture"}}, call_id=f"turn-{iteration}"
        )
        await rail.before_tool_call(ctx)
        assert ctx.extra.get("_skip_tool")
    adapter._agent_permissions_body = {
        **adapter._agent_permissions_body,
        "tools": {"web_flash": "allow", "web_search": "allow"},
    }
    adapter._update_permission_rail({}, session_id="a")
    ctx = tool_context("web_flash", {"search": {"query": "fixture"}})
    await rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")
    assert id(adapter._permission_rail) == identity


def test_disabled_to_enabled_creates_flash_rail(permission_adapter):
    adapter, _ = permission_adapter({"web_search": "deny"})
    adapter._permission_rail = None
    skill_rail = adapter._build_skill_authorization_rail()
    adapter._skill_authorization_rail = skill_rail
    assert skill_rail._engine is None
    adapter._update_permission_rail({}, session_id="a")
    assert isinstance(adapter._permission_rail, FlashPermissionRail)
    assert adapter._skill_authorization_rail is skill_rail
    assert skill_rail._engine is adapter._permission_rail._engine


@pytest.mark.parametrize("reload", [False, True])
def test_enabled_permission_build_failure_stops_flash(
    permission_adapter, monkeypatch, reload
):
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers

    adapter, _ = permission_adapter({"write_file": "deny"})
    monkeypatch.setattr(interrupt_helpers, "build_permission_rail", lambda **_: None)
    adapter._permission_rail = None
    with pytest.raises(RuntimeError, match="Flash permission rail"):
        if reload:
            adapter._update_permission_rail({}, session_id="a")
        else:
            adapter._build_permission_rail_for_agent(config={})
    adapter._agent_permissions_body = {"enabled": False}
    assert adapter._build_permission_rail_for_agent(config={}) is None


@pytest.mark.asyncio
async def test_permission_refresh_failure_never_calls_backend(permission_adapter):
    _, rail = permission_adapter({"web_flash": "allow", "web_search": "allow"})

    def fail():
        raise RuntimeError("fixture storage unavailable")

    rail._host.get_permissions_snapshot = fail
    ctx = tool_context("web_flash", {"search": {"query": "fixture"}})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
async def test_flash_credential_rails_bind_independent_session_envs(
    permission_adapter, monkeypatch
):
    from jiuwenswarm.agents.harness.common.rails import (
        skill_credential_injection_rail as credentials,
    )

    adapter_a, _ = permission_adapter({"bash": "allow"})
    adapter_b, _ = permission_adapter({"bash": "allow"})
    adapter_a._parent_session_id = "a"
    adapter_b._parent_session_id = "b"
    rail_a = adapter_a._build_skill_credential_injection_rail(
        {"skill_envs": {"fixture": {"SKILL_TEST_TOKEN": "sentinel-a"}}}
    )
    rail_b = adapter_b._build_skill_credential_injection_rail(
        {"skill_envs": {"fixture": {"SKILL_TEST_TOKEN": "sentinel-b"}}}
    )
    seen = []

    def active(session_id):
        seen.append(session_id)
        return "fixture"

    monkeypatch.setattr(credentials, "get_session_active_skill", active)
    ca = tool_context("bash", {"command": "echo fixture"}, session_id="a")
    cb = tool_context("bash", {"command": "echo fixture"}, session_id="b")
    await asyncio.gather(rail_a.before_tool_call(ca), rail_b.before_tool_call(cb))
    import json

    def env(ctx):
        value = ctx.inputs.tool_args
        return (json.loads(value) if isinstance(value, str) else value)["env"]

    assert env(ca)["SKILL_TEST_TOKEN"] == "sentinel-a"
    assert env(cb)["SKILL_TEST_TOKEN"] == "sentinel-b"
    assert set(seen) == {"a", "b"}


@pytest.mark.asyncio
async def test_native_deepagent_cold_and_reload_register_flash_rail_once(
    permission_adapter, monkeypatch
):
    from uuid import uuid4

    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig

    adapter, _ = permission_adapter({"web_flash": "allow", "web_search": "deny"})
    rails = adapter._instantiate_rails(
        [
            _RailBuildInfo(
                "_permission_rail",
                adapter._build_permission_rail_for_agent,
                params={"config": {}},
            ),
        ],
        {},
    )
    permission = adapter._permission_rail
    observed = []
    original = permission.before_tool_call

    async def before(ctx):
        observed.append(ctx.inputs.tool_call.id)
        await original(ctx)

    monkeypatch.setattr(permission, "before_tool_call", before)
    agent = DeepAgent(AgentCard(id="flash-permission-test-" + uuid4().hex, name="flash-test"))
    adapter._instance = agent
    agent.configure(DeepAgentConfig(rails=rails, enable_read_image_multimodal=False))
    try:
        await agent.ensure_initialized()
        for iteration in range(3):
            if iteration == 2:
                adapter._update_permission_rail({}, session_id="a")
                agent.configure(
                    DeepAgentConfig(rails=[], enable_read_image_multimodal=False)
                )
                await agent.ensure_initialized()
            ctx = tool_context(
                "web_flash",
                {"search": {"query": "fixture"}},
                call_id=f"sdk-turn-{iteration}",
            )
            await agent._react_agent.agent_callback_manager.execute(
                AgentCallbackEvent.BEFORE_TOOL_CALL, ctx
            )
            assert ctx.extra.get("_skip_tool")
            assert observed.count(f"sdk-turn-{iteration}") == 1
        assert adapter._permission_rail is permission
    finally:
        for registered in list(agent._registered_rails):
            await agent.unregister_rail(registered)
        await agent.agent_callback_manager.clear()
        await agent._react_agent.agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_parallel_denial_does_not_skip_an_allowed_sibling(permission_adapter):
    from unittest.mock import patch

    from jiuwenswarm.agents.harness.flash.tools.web_flash import (
        JiuwenHarnessFetchWebpageTool,
        JiuwenHarnessWebSearchTool,
        build_web_flash_tool,
    )

    _, rail = permission_adapter(
        {"web_flash": "allow", "web_search": "allow", "mcp_fetch_webpage": "deny"}
    )
    rejected, completed = asyncio.Event(), asyncio.Event()
    original = rail.before_tool_call

    async def before(ctx):
        await original(ctx)
        if ctx.inputs.tool_call.id == "denied":
            rejected.set()
            await completed.wait()
        else:
            await rejected.wait()

    rail.before_tool_call = before

    async def search(*args, **kwargs):
        completed.set()
        return "allowed fixture"

    calls = [
        tool_context(
            "web_flash",
            {"fetch": {"url": ["https://example.invalid/a"]}},
            call_id="denied",
        ).inputs.tool_call,
        tool_context(
            "web_flash", {"search": {"query": "fixture"}}, call_id="allowed"
        ).inputs.tool_call,
    ]
    session = tool_context("web_flash", {}).session
    with (
        patch.object(
            JiuwenHarnessWebSearchTool, "invoke", new=AsyncMock(side_effect=search)
        ) as backend,
        patch.object(JiuwenHarnessFetchWebpageTool, "invoke", new=AsyncMock()) as fetch,
    ):
        async with sdk_tools(
            rail, session, [build_web_flash_tool(agent_id="flash-test")]
        ) as (manager, ctx):
            results = await asyncio.wait_for(manager.execute(ctx, calls, session), 5)
            backend.assert_awaited_once()
            fetch.assert_not_awaited()
            assert "PERMISSION_DENIED" in str(results[0])
            assert "allowed fixture" in str(results[1])


@pytest.mark.asyncio
async def test_allowed_bash_reaches_native_backend_once(permission_adapter):
    _, rail = permission_adapter({"bash": "allow"})
    backend = AsyncMock(return_value="fixture")

    async def run(command):
        return await backend(command=command)

    tool = LocalFunction(
        card=ToolCard(
            name="bash",
            id="bash",
            description="Harmless command backend",
            input_params={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        func=run,
    )
    ctx = tool_context("bash", {"command": "echo fixture"}, session_id="a")
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        backend.assert_awaited_once_with(command="echo fixture")


@pytest.mark.asyncio
async def test_todo_native_dispatch_keeps_each_session(permission_adapter, tmp_path):
    from types import SimpleNamespace

    from openjiuwen.harness.tools.base_tool import ToolOutput

    from jiuwenswarm.agents.harness.flash.tools.flash_todo import UnifiedTodoTool

    seen = []

    async def create(inputs, **kwargs):
        session_id = kwargs["session"].get_session_id()
        (tmp_path / f"{session_id}.json").write_text(str(inputs))
        seen.append(session_id)
        return ToolOutput(success=True, data=inputs)

    tool = UnifiedTodoTool(
        {"create": SimpleNamespace(invoke=create)}, "cn", agent_id="flash-test"
    )
    _, rail = permission_adapter({"todo": "allow", "todo_create": "allow"})
    a = tool_context(
        "todo",
        {"action": "create", "tasks": [{"id": "task-a", "content": "A"}]},
        session_id="a",
        call_id="a",
    )
    b = tool_context(
        "todo",
        {"action": "create", "tasks": [{"id": "task-b", "content": "B"}]},
        session_id="b",
        call_id="b",
    )
    async with sdk_tools(rail, a.session, [tool]) as (manager, native_ctx):
        await asyncio.gather(
            manager.execute(native_ctx, a.inputs.tool_call, a.session),
            manager.execute(native_ctx, b.inputs.tool_call, b.session),
        )
        assert sorted(seen) == ["a", "b"]
        assert "task-a" in (tmp_path / "a.json").read_text()
        assert "task-b" not in (tmp_path / "a.json").read_text()
        assert "task-b" in (tmp_path / "b.json").read_text()


@pytest.mark.parametrize("reload", [False, True])
def test_shared_deep_and_code_builder_is_unchanged(permission_adapter, reload):
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter, _ = permission_adapter({"write_file": "deny"})
    for adapter_type in (JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter):
        deep = adapter_type()
        deep._agent_permissions_body = adapter._agent_permissions_body
        if reload:
            deep._update_permission_rail({})
            rail = deep._permission_rail
        else:
            rail = deep._build_permission_rail_for_agent(config={})
        assert rail is not None
        assert not isinstance(rail, FlashPermissionRail)
    assert (
        JiuwenSwarmCodeAdapter._build_permission_rail_for_agent
        is JiuWenSwarmDeepAdapter._build_permission_rail_for_agent
    )


@pytest.mark.asyncio
async def test_concurrent_workspaces_deny_foreign_target_and_keep_next_turn(
    permission_adapter, tmp_path
):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    rails = [
        permission_adapter(
            {"write_file": "allow"},
            file_guard={
                "enabled": True,
                "defaults": {"write": "deny"},
                "paths": [{"path": str(root), "write": "allow"}],
            },
        )[1]
        for root in roots
    ]
    a, b = [root / "result.txt" for root in roots]
    writes_a, writes_b = [], []
    ca = tool_context("write_file", {"file_path": str(a)}, session_id="a", call_id="a")
    cb = tool_context("write_file", {"file_path": str(b)}, session_id="b", call_id="b")
    async with (
        sdk_tools(rails[0], ca.session, [writing_tool(a, writes_a)]) as (ma, ctxa),
        sdk_tools(rails[1], cb.session, [writing_tool(b, writes_b)]) as (mb, ctxb),
    ):
        results = await asyncio.gather(
            ma.execute(ctxa, ca.inputs.tool_call, ca.session),
            mb.execute(ctxb, cb.inputs.tool_call, cb.session),
        )
        assert str(a) in str(results[0]) and str(b) not in str(results[0])
        assert str(b) in str(results[1]) and str(a) not in str(results[1])
        forged = ca.inputs.tool_call.model_copy(update={"id": "b-forged"})
        denied = await mb.execute(ctxb, forged, cb.session)
        assert "PERMISSION_DENIED" in str(denied)
        assert writes_b == [str(b)]
        await ma.execute(
            ctxa, ca.inputs.tool_call.model_copy(update={"id": "a-next"}), ca.session
        )
        assert writes_a == [str(a), str(a)]
        assert a.read_text() == b.read_text() == "fixture"


@pytest.mark.asyncio
async def test_cancel_a_while_b_backend_is_running(permission_adapter, tmp_path):
    _, rail_a = permission_adapter({"write_file": "ask"})
    _, rail_b = permission_adapter({"write_file": "allow"})
    a_waiting, b_running, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def confirm(_):
        a_waiting.set()
        await asyncio.Event().wait()

    rail_a._host.request_permission_confirmation = confirm
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    writes_a, writes_b = [], []

    async def slow_write(file_path):
        b_running.set()
        await release_b.wait()
        writes_b.append(file_path)
        Path(file_path).write_text("B continues")
        return file_path

    tool_b = LocalFunction(card=writing_tool(b, []).card, func=slow_write)
    ca = tool_context("write_file", {"file_path": str(a)}, session_id="a")
    cb = tool_context("write_file", {"file_path": str(b)}, session_id="b")
    async with (
        sdk_tools(rail_a, ca.session, [writing_tool(a, writes_a)]) as (ma, ctxa),
        sdk_tools(rail_b, cb.session, [tool_b]) as (mb, ctxb),
    ):
        task_a = asyncio.create_task(ma.execute(ctxa, ca.inputs.tool_call, ca.session))
        await asyncio.wait_for(a_waiting.wait(), 5)
        task_b = asyncio.create_task(mb.execute(ctxb, cb.inputs.tool_call, cb.session))
        await asyncio.wait_for(b_running.wait(), 5)
        task_a.cancel()
        await asyncio.gather(task_a, return_exceptions=True)
        assert not task_b.done()
        release_b.set()
        await asyncio.wait_for(task_b, 5)
        assert writes_a == [] and not a.exists()
        assert writes_b == [str(b)] and b.read_text() == "B continues"
