# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 20 small test modules)."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from copy import deepcopy
from jiuwenswarm.runtime import kernel as kernel_module
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.worker.preparation import (
    PreparationRegistry,
    current_preparation_registry,
)
import os
import psutil
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.supervisor.resident_lifecycle import WorkerShutdownIncomplete
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from unittest.mock import MagicMock
from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
    CodeConfirmInterruptRail,
)
from typing import Any
from openjiuwen.harness.tools.cron import CronToolContext
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _QUARTZ_DOW_DECLARATION_PATTERNS,
    CronRuntimeBridge,
)
import re
import ast
import importlib.util
import sys
import agent_gateway
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
import subprocess
from websockets.legacy.client import connect
from tests.unit_tests.agentserver.fixtures.front_process import free_port, receive, seed, terminate_owned
from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from openjiuwen.core.foundation.llm import ToolMessage
from jiuwenswarm.agents.harness.common.session_ops_service import _build_context_messages_from_history
from e2a.message import EventType
from agent_gateway.channel_manager.tui.tui_channel import TuiChannel
from agent_gateway.channel_manager.web.web_connect import WebChannel
from agent_gateway.message_handler.message_handler import MessageHandler
from e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.server import agent_ws_server
from unittest.mock import Mock
from minio.error import S3Error
from jiuwenswarm.common.obs_files import AttachmentStore
import warnings
from datetime import datetime, timezone
from jiuwenswarm.server.runtime.skill.skilldev.schema import _now_iso
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from openjiuwen.core.common.background_tasks import BackgroundTask
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _drain_ttse_rail_background_tasks
from jiuwenswarm.server.worker import runtime_backend


# --- from test_a4p_split_process.py ---

def _gateway_process(connection, directory):
    async def run():
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from e2a.wire_codec import parse_agent_server_wire_chunk
        from jiuwenswarm.common.schema.message import ReqMethod
        from agent_gateway.cron.controller import CronController
        from agent_gateway.cron.store import CronJobStore
        from agent_gateway.message_handler.message_handler import MessageHandler

        store = CronJobStore(path=Path(directory) / "jobs.json")
        await store.create_job(job_id="remote", name="remote", cron_expr="0 9 * * *",
                               timezone="UTC", description="test", targets="web", user_id="alice")
        handler = object.__new__(MessageHandler)
        handler._stream_metadata = {}

        class Client:
            async def send_request(self, envelope, **kwargs):
                connection.send(json.dumps(envelope.to_dict()))
                return SimpleNamespace(ok=True, payload={})

        handler.agent_client = Client()
        controller = CronController(
            store=store, scheduler=SimpleNamespace(reload=AsyncMock())
        )
        handler._cron_controller = controller

        async def ack(*, command_id, data, user_id=None, session_id=None):
            envelope = e2a_from_agent_fields(
                request_id=command_id, channel_id="", req_method=ReqMethod.CRON_COMMAND_ACK,
                params={"command_id": command_id, "data": data}, user_id=user_id,
            )
            connection.send(json.dumps(envelope.to_dict()))

        handler._push_cron_command_ack = ack
        connection.send("ready")
        while True:
            raw = connection.recv()
            if raw == "stop":
                return
            wire = json.loads(raw)
            chunk = parse_agent_server_wire_chunk(wire)
            await handler._handle_cron_push_payload(
                payload=chunk.payload, request_id=chunk.request_id, channel_id="",
                session_id=None, metadata=None,
                user_id=wire["metadata"].get("_jiuwenswarm_cron_owner_user_id"),
            )
    try:
        asyncio.run(run())
    finally:
        connection.close()

@pytest.mark.asyncio
async def test_remote_query_does_not_read_agent_store(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.cron import cron_tools
    from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import query_authoritative_cron_job
    from jiuwenswarm.runtime.host_services import install_runtime_push_handler, restore_runtime_push_handler
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from e2a.models import E2AEnvelope
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    gateway_dir, agent_dir = tmp_path / "gateway", tmp_path / "agent"
    gateway_dir.mkdir()
    agent_dir.mkdir()
    monkeypatch.setattr(cron_tools, "get_cron_jobs_path", lambda: agent_dir / "jobs.json")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_gateway_process, args=(child, str(gateway_dir)))
    process.start()
    child.close()
    server = object.__new__(AgentWebSocketServer)
    ws = SimpleNamespace(send=AsyncMock())

    async def receive():
        assert await asyncio.to_thread(parent.poll, 30), "Gateway did not respond"
        return parent.recv()

    async def push(message):
        parent.send(json.dumps(build_server_push_wire(message)))
        request = e2a_to_agent_request(E2AEnvelope.from_dict(json.loads(await receive())))
        await server._handle_gateway_cron_callback(ws, request, asyncio.Lock())
        return True

    previous = install_runtime_push_handler(push)
    try:
        assert await receive() == "ready"
        job = await query_authoritative_cron_job("remote", user_id="alice")
        assert job["id"] == "remote"
        with pytest.raises(RuntimeError, match="forbidden"):
            await query_authoritative_cron_job("remote", user_id="bob")
        assert await query_authoritative_cron_job("missing", user_id="alice") is None
        assert not (agent_dir / "jobs.json").exists()
    finally:
        restore_runtime_push_handler(push, previous)
        if process.is_alive():
            parent.send("stop")
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
    assert process.exitcode == 0

async def test_runtime_handler_uses_shared_a4p_dispatch(monkeypatch):
    """Runtime and WebSocket requests must use the same A4P implementation."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jiuwenswarm.agents.harness.common.a4p import rpc
    from jiuwenswarm.server.handlers.organization import handle_a4p_request

    request = object()
    response = object()
    dispatch = AsyncMock(return_value=response)
    monkeypatch.setattr(rpc, "dispatch_a4p_request", dispatch)
    sink = SimpleNamespace(send_response=AsyncMock())
    await handle_a4p_request(SimpleNamespace(request=request, sink=sink))
    dispatch.assert_awaited_once_with(request)
    sink.send_response.assert_awaited_once_with(response)

# --- from test_adapter_kernel_invalidation.py ---

@pytest.mark.asyncio
async def test_permissions_change_refreshes_model_lease_without_closing_an_existing_lease(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    kernel = kernel_module.ResourceKernel()
    monkeypatch.setattr(kernel_module, "process_kernel", lambda: kernel)
    built = []

    def build_model(mcc, mco):
        model = SimpleNamespace(model_client_config=mcc, model_config=mco)
        built.append(model)
        return model

    monkeypatch.setattr(interface_deep, "build_model_from_entry", build_model)
    monkeypatch.setattr(interface_deep, "is_placeholder_model_entry", lambda _: False)
    monkeypatch.setattr(interface_deep, "model_client_config_view", lambda value: value)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._global_index_to_cache_key = {}
    adapter._model_id_to_key = {}
    adapter._model_group_cache = {}
    adapter._last_models_config_fingerprint = None
    adapter._model = None
    monkeypatch.setattr(adapter, "_inject_attribution_to_config", lambda _: None)

    def build_defaults(_config):
        adapter._model_cache["model-a"] = adapter._build_kernel_model(
            {"model_name": "model-a", "api_base": "http://model.invalid"}, {},
            resource_id="stable-model-a",
        )

    monkeypatch.setattr(adapter, "_build_model_cache_from_defaults", build_defaults)
    config = {"models": {}, "permissions": {"mode": "ask"}}
    first = adapter._create_model(deepcopy(config))
    old_lease = next(iter(adapter._kernel_leases.values()))
    assert adapter._create_model(deepcopy(config)) is first
    assert len(built) == 1

    config["permissions"]["mode"] = "auto"
    second = adapter._create_model(deepcopy(config))
    assert second is not first
    assert old_lease.value is first
    assert len(built) == 2
    assert adapter._create_model(deepcopy(config)) is second
    assert len(built) == 2
    for lease in adapter._kernel_leases.values():
        await lease.release()
    await kernel.close()

# --- from test_adapter_prewarm_admission.py ---

@pytest.mark.asyncio
async def test_root_prewarm_cannot_bypass_foreground_pause_or_cancel_shared_owner():
    began = asyncio.Event()
    finish = asyncio.Event()

    async def warm():
        began.set()
        await finish.wait()

    owner = PreparationRegistry()
    owner.register("mcp", warm)
    owner.bind_current_loop()
    token = owner.pause_background()
    owner.mark_minimum_ready()
    adapter = SimpleNamespace(
        _mcp_prewarm_task=None, _agent_name="first-chat", _do_mcp_prewarm=AsyncMock()
    )
    try:
        JiuWenSwarmDeepAdapter._start_mcp_prewarm(adapter)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not began.is_set()
        adapter._do_mcp_prewarm.assert_not_awaited()
        owner.resume_background(token)
        await asyncio.wait_for(began.wait(), timeout=1)
        shared = owner.start("mcp")
        adapter._mcp_prewarm_task.cancel()
        await asyncio.gather(adapter._mcp_prewarm_task, return_exceptions=True)
        assert not shared.cancelled()
        finish.set()
        await asyncio.wait_for(shared, timeout=1)
        assert owner.snapshot()["mcp"] == "ready"
    finally:
        await owner.close()
    assert current_preparation_registry() is None

# --- from test_attached_worker_final_shutdown.py ---

async def test_attached_ack_does_not_hide_a_live_dependency_thread(monkeypatch, tmp_path):
    shim = tmp_path / "linger"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from jiuwenswarm.server.worker import stub\n"
        "    original = stub._serve_one\n"
        "    async def serve(connection, **kwargs):\n"
        "        result = await original(connection, **kwargs)\n"
        "        if result is True:\n"
        "            import threading, time\n"
        "            threading.Thread(target=lambda: time.sleep(60), daemon=False).start()\n"
        "        return result\n"
        "    stub._serve_one = serve\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(source)]))
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    supervisor = RuntimeSupervisor(Readiness(), stub=True, max_restarts=0, connect_timeout=8)
    process = None
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await supervisor._readiness.wait_agent_ready(5)
        pid = supervisor.worker_pid
        process = supervisor._process
        monkeypatch.setattr(runtime_supervisor, "_FINAL_SHUTDOWN_TIMEOUT", 1.5)
        with pytest.raises(WorkerShutdownIncomplete, match="forced"):
            await supervisor.stop()
        assert await process.wait(timeout=2) is not None
        assert not psutil.pid_exists(pid)
    finally:
        await supervisor.stop()
        if process is not None:
            await process.stop()

# --- from test_code_confirm_interrupt_rail.py ---

@pytest.mark.asyncio
async def test_switch_mode_exit_rejected_in_plan_without_confirm_ui() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    agent.system_prompt_builder = SimpleNamespace(language="cn")

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=agent, session=SimpleNamespace())

    decision = await rail.resolve_interrupt(ctx, tool_call, user_input=None)

    assert isinstance(decision, RejectResult)
    assert "switch_mode" in str(decision.tool_result)

@pytest.mark.asyncio
async def test_switch_mode_exit_rejected_even_after_user_approves_confirm() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    agent.system_prompt_builder = SimpleNamespace(language="cn")

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=agent, session=SimpleNamespace())

    decision = await rail.resolve_interrupt(
        ctx, tool_call, user_input={"approved": True}
    )

    assert isinstance(decision, RejectResult)

# --- from test_cron_dow_schema_fix.py ---

class _DummyCronBackend:
    """满足 CronToolBackend 协议的最小桩；create_cron_tools 构造工具时不调用任何方法。"""

    async def list_jobs(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def create_job(self, params: dict[str, Any], *, context: CronToolContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def update_job(self, job_id: str, patch: dict[str, Any], *, context: CronToolContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def delete_job(self, job_id: str) -> bool:
        raise NotImplementedError

    async def toggle_job(self, job_id: str, enabled: bool) -> dict[str, Any]:
        raise NotImplementedError

    async def preview_job(self, job_id: str, count: int = 5) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def run_now(self, job_id: str) -> str:
        raise NotImplementedError

    async def status(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        raise NotImplementedError

def _build_tools(language: str) -> list[Any]:
    bridge = CronRuntimeBridge()
    bridge.set_backend(_DummyCronBackend())
    return bridge.build_tools(
        context=CronToolContext(channel_id="web", session_id="sess_test"),
        agent_id="test-agent",
        language=language,
    )

def _collect(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_collect(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect(item))
    return out

def _all_text(tools: list[Any]) -> list[str]:
    out: list[str] = []
    for tool in tools:
        card = getattr(tool, "card", None)
        if card is None:
            continue
        out.append(str(getattr(card, "description", None) or ""))
        out.extend(_collect(card.input_params))
    return out

def _has_quartz_declaration(text: str) -> bool:
    return any(pattern.search(text) for pattern in _QUARTZ_DOW_DECLARATION_PATTERNS)

@pytest.mark.parametrize(
    ("language", "dow_marker"),
    [
        ("cn", "0=周日"),
        ("en", "0=SUN"),
    ],
)
def test_build_tools_cards_have_croniter_dow_semantics(language: str, dow_marker: str) -> None:
    tools = _build_tools(language)
    texts = _all_text(tools)
    assert not any(_has_quartz_declaration(text) for text in texts), \
        f"{language} 工具描述仍残留 Quartz 语义声明"
    assert any(dow_marker in text for text in texts), \
        f"{language} 工具描述缺少 {dow_marker} 语义说明"
    assert any("WED,FRI" in text for text in texts), \
        f"{language} 工具描述缺少字母缩写示例 WED,FRI"

# --- from test_deep_adapter_web_tool_language.py ---

_CJK = re.compile(r"[\u4e00-\u9fff]")

_WEB_TOOL_NAMES = ("free_search", "fetch_webpage")

def _make_adapter(
    monkeypatch, preferred_language: str
) -> tuple[JiuWenSwarmDeepAdapter, dict[str, Any]]:
    """Create a bare deep adapter whose tool discovery registers nothing optional."""
    config = {"preferred_language": preferred_language}
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_deep, "is_paid_search_enabled", lambda: False)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    adapter._is_session_scoped_adapter = False
    adapter._config_base_cache = config
    adapter._skill_manager = None
    adapter._vision_model_config = None
    adapter._audio_model_config = None
    adapter._video_model_config = None
    adapter._image_gen_model_config = None
    adapter._video_gen_model_config = None
    adapter._visual_gen_model_config = None
    adapter._xiaoyi_phone_tools_registered = False
    adapter._instance = None
    adapter._is_session_scoped_adapter = False
    adapter._tool_retrieval_session_enabled = False
    adapter._iter_runtime_audio_tools = lambda agent_id: []
    adapter._skill_retrieval_tools_enabled_for_runtime = lambda config_base: False
    adapter._tool_retrieval_tools_enabled_for_runtime = lambda config_base: False
    adapter._register_shared_tool = lambda tool: None
    registered: dict[str, Any] = {}
    adapter._register_agent_owned_tool = lambda tool, owner_id: registered.update(
        {tool.card.name: tool}
    )
    return adapter, registered

def _schema_text(card: Any) -> str:
    return f"{card.description}\n{card.input_params}"

@pytest.mark.asyncio
async def test_agent_mode_web_tools_use_english_schema_for_en(monkeypatch):
    adapter, registered = _make_adapter(monkeypatch, "en")

    await adapter._get_tool_cards("agent-1")

    for name in _WEB_TOOL_NAMES:
        schema = _schema_text(registered[name].card)
        assert not _CJK.search(schema), f"{name} schema is not English: {schema}"

@pytest.mark.asyncio
async def test_agent_mode_web_tools_keep_chinese_schema_for_zh(monkeypatch):
    adapter, registered = _make_adapter(monkeypatch, "zh")

    await adapter._get_tool_cards("agent-1")

    for name in _WEB_TOOL_NAMES:
        assert _CJK.search(registered[name].card.description), name

# --- from test_enterprise_skill_http_route.py ---

_ROOT = Path(__file__).resolve().parents[3]

_GATEWAY_ROOT = Path(agent_gateway.__file__).resolve().parent

def _mapped_routes():
    path = _GATEWAY_ROOT / "channel_manager/web/http_routes/mapped.py"
    spec = importlib.util.spec_from_file_location("enterprise_skill_mapped_routes", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MAPPED_ROUTES

def _assigned_handler(source: Path, enum_name: str) -> str:
    text = source.read_text(encoding="utf-8")
    needle = f"ReqMethod.{enum_name}:"
    for line in text.splitlines():
        if needle in line and "handle_" in line:
            return line.split(":", 1)[1].strip().strip('",')
    raise AssertionError(f"{enum_name} is not in _SKILL_ROUTES")

def _forward_sets_contain(method: str) -> bool:
    source = _GATEWAY_ROOT / "channel_manager/web/app_web_handlers.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = {name: False for name in ("_FORWARD_REQ_METHODS", "_FORWARD_NO_LOCAL_HANDLER_METHODS")}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in found:
                blob = ast.get_source_segment(source.read_text(encoding="utf-8"), node.value) or ""
                found[target.id] = method in blob
    return all(found.values())

def test_enterprise_install_http_route_reaches_web_install_handler(tmp_path) -> None:
    method = "skills.enterprise.install"
    interface = _ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface.py"
    handler_name = _assigned_handler(interface, "SKILLS_ENTERPRISE_INSTALL")
    from jiuwenswarm.common.schema.message import ReqMethod
    assert ReqMethod.SKILLS_ENTERPRISE_INSTALL.value == method
    assert handler_name == "handle_skills_web_install"
    assert _forward_sets_contain(method)
    matched = [
        route for route in _mapped_routes()
        if route.rpc_method == method and route.path == "/skills/enterprise/actions/install"
    ]
    assert matched and matched[0].http_method == "POST"

    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    seen: dict[str, object] = {}

    async def _install(params: dict) -> dict:
        seen["params"] = params
        return {"success": True, "skill": {"name": "from-http"}}

    manager.handle_skills_web_install = _install  # type: ignore[method-assign]
    payload = asyncio.run(getattr(manager, handler_name)({"url": "https://skills.example/pkg.zip"}))
    assert payload["success"] is True
    assert seen["params"]["url"] == "https://skills.example/pkg.zip"

# --- from test_execution_import_boundaries.py ---

@pytest.mark.parametrize(
    "statement, blocked",
    [
        (
            "from jiuwenswarm.common.auth import login_credentials",
            ["agent_gateway.auth.account_kit"],
        ),
        (
            "from jiuwenswarm.agents.harness.common.rails.permissions import tool_permission_context",
            [
                "jiuwenswarm.agents.harness.common.rails.symphony",
                "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail",
            ],
        ),
        (
            "from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image; assert generate_image.card.name == 'generate_image'",
            ["fastmcp", "google.genai"],
        ),
    ],
)
def test_unrelated_optional_imports_are_not_required(statement, blocked, tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc, sys
sys.path.insert(0, {str(root)!r})
blocked = {blocked!r}
class RejectOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('optional module required: ' + fullname)
sys.meta_path.insert(0, RejectOptional())
{statement}
assert not any(name in sys.modules for name in blocked)
print('EXECUTION_IMPORT_BOUNDARY_OK')
"""
    env = dict(
        os.environ,
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXECUTION_IMPORT_BOUNDARY_OK" in result.stdout

# --- from test_front_import_boundary.py ---

_OBSERVER = '''
import importlib.abc
import importlib.machinery
import json
import os
import sys
class Observer(importlib.abc.MetaPathFinder):
    baseline = set()
    def find_spec(self, name, path=None, target=None):
        if name != "jiuwenswarm.server.front.router":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                dispatch = module.MethodRouter._dispatch_control
                async def observed(instance, ws, request, lock):
                    await dispatch(instance, ws, request, lock)
                    prefixes = ("openjiuwen", "mcp", "jiuwenswarm.agents.harness",
                        "jiuwenswarm.server.runtime.agent_adapter",
                        "jiuwenswarm.server.runtime.skill.skill_manager", "jiuwenswarm.runtime.project_lsp",
                        "jiuwenswarm.server.runtime.session.kv_cache")
                    hits = {name for name in sys.modules if any(name == p or name.startswith(p + ".") for p in prefixes)}
                    if not Observer.baseline:
                        Observer.baseline.update(hits)
                    delta = sorted(hits - Observer.baseline)
                    with open(os.environ["ISSUE4689_MODULE_OBSERVER"], "a", encoding="utf-8") as file:
                        file.write(json.dumps({"pid": os.getpid(), "method": request.req_method.value,
                            "forbidden": delta, "loaded_modules": sorted(sys.modules)}) + "\\n")
                module.MethodRouter._dispatch_control = observed
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Observer())
'''

@pytest.mark.asyncio
async def test_native_front_control_queries_do_not_load_runtime_dependencies(tmp_path: Path):
    root = Path(__file__).resolve().parents[3]
    data = tmp_path / "data"
    seed(data, root, "agent")
    observer = tmp_path / "observer"
    observer.mkdir()
    (observer / "sitecustomize.py").write_text(_OBSERVER, encoding="utf-8")
    observations = tmp_path / "modules.jsonl"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(observer), str(root))),
           "JIUWENSWARM_DATA_DIR": str(data), "ISSUE4689_MODULE_OBSERVER": str(observations)}
    env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
    env.pop("JIUWENSWARM_RUNTIME_STUB", None)
    port = free_port()
    with (tmp_path / "front.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(port)],
                                   cwd=root, env=env, stdout=log, stderr=log)
        ws = None
        try:
            for _ in range(400):
                try:
                    ws = await connect(f"ws://127.0.0.1:{port}")
                    acknowledgement = json.loads(await ws.recv())
                    assert acknowledgement.get("event") == "connection.ack"
                    break
                except OSError:
                    await asyncio.sleep(.01)
            assert ws is not None
            for method, params in (("session.list", {}), ("project.list", {}), ("config.get", {}),
                                   ("history.get", {"session_id": "acceptance-0", "page_idx": 1})):
                await ws.send(json.dumps({"request_id": method, "channel_id": "web", "req_method": method,
                                          "session_id": "acceptance-0", "params": params}))
                await receive(ws, method)
            # The observer appends each ~7.7KB JSON line via a buffered write;
            # splitlines() counts a torn (mid-flush) line as complete, so a
            # count-based wait can race the writer and hand json.loads a
            # truncated string. Retry until the whole file parses cleanly into
            # the expected row count instead.
            rows: list[dict] = []
            for _ in range(400):
                if observations.exists():
                    try:
                        rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
                    except json.JSONDecodeError:
                        rows = []
                    if len(rows) == 4:
                        break
                await asyncio.sleep(.01)
            assert len(rows) == 4
            assert len({row["pid"] for row in rows}) == 1
            assert all(row["forbidden"] == [] for row in rows)
        finally:
            if ws is not None:
                await ws.close()
            terminate_owned(process, [])

# --- from test_group_chat_language.py ---

def team_config(language):
    return {
        'preferred_language': language,
        'modes': {'team': {'test': {'agents': {'leader': {}, 'teammate': {}}}}},
    }

@pytest.mark.parametrize('language, expected', [('zh', 'cn'), (' ZH ', 'cn'), ('cn', 'cn'), ('en', 'en')])
def test_team_config_uses_core_language(language, expected, monkeypatch):
    monkeypatch.setenv('AGENT_PROMPT_LANGUAGE', 'en')
    config = team_config(language)
    spec = load_team_spec_dict(config_base=config)
    assert spec['language'] == expected
    assert config['preferred_language'] == language

@pytest.mark.asyncio
async def test_chinese_group_mention_builds_real_core_context(monkeypatch):
    from openjiuwen.agent_teams.group_chat import handler

    spec = load_team_spec_dict(config_base=team_config('zh'))
    row = SimpleNamespace(
        message_id='trigger', team_name='group', from_member_name='user', content='@expert hello', timestamp=10,
        meta=json.dumps({
            'type': 'group_chat', 'client_message_id': 'message', 'sender_name': 'User',
            'mentions': ['expert'], 'attachments': [],
        }),
    )
    conversation = SimpleNamespace(session_id='group', history_path=Path('/tmp/history.jsonl'))
    backend = SimpleNamespace(
        group_chat_spec=SimpleNamespace(language=spec['language']), team_name='group', message_manager=None,
        group_conversation=AsyncMock(return_value=conversation),
        db=SimpleNamespace(message=SimpleNamespace(get_broadcast_read_at=AsyncMock(return_value=0))),
    )
    monkeypatch.setattr(handler, 'sync_history', AsyncMock(return_value=[]))
    context = await handler.context_for(backend, 'expert', row)
    assert '@expert hello' in context
    assert 'trigger' in context

# --- from test_history_tool_result_restore.py ---

def _records(tool_result: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "tool_call": {"name": "glob", "tool_call_id": "tc-1", "arguments": {"pattern": "*.py"}},
        },
        {"role": "assistant", "event_type": "chat.tool_result", "tool_call_id": "tc-1", **tool_result},
    ]

def _tool_messages(messages: list[object]) -> list[ToolMessage]:
    return [message for message in messages if isinstance(message, ToolMessage)]

def test_restore_prefers_rendered_result() -> None:
    messages, _ = _build_context_messages_from_history(
        _records({"result": "success=True data={'matching_files': ['/a.py']} error=None", "rendered_result": "/a.py"})
    )

    assert [message.content for message in _tool_messages(messages)] == ["/a.py"]

def test_restore_falls_back_to_legacy_result() -> None:
    messages, _ = _build_context_messages_from_history(_records({"result": "legacy text"}))

    assert [message.content for message in _tool_messages(messages)] == ["legacy text"]

# --- from test_mode_cross_layer_protocol.py ---

class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

class _ProtocolAgent:
    def __init__(
        self,
        *,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        self._payload = payload
        self._metadata = metadata

    async def process_message_stream(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=self._payload,
            metadata=self._metadata,
        )
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=None,
            is_complete=True,
        )

class _RuntimeManager:
    def __init__(self) -> None:
        self.foreground_calls: list[str] = []

    async def begin_foreground_chat(self) -> None:
        self.foreground_calls.append("begin")

    async def end_foreground_chat(self) -> None:
        self.foreground_calls.append("end")

    async def cancel_all_inflight_work(self, reason: str) -> None:
        return None

    async def cleanup(self) -> None:
        return None

class _PlanController:
    async def ensure_state(self, *args: object) -> PlanStateResult:
        return PlanStateResult()

    async def check_post_process_exit(self, *args: object) -> list[dict[str, Any]]:
        return []

_MODE_EVENT_CASES = (
    pytest.param(
        "web",
        {"mode": "team", "id": "leader"},
        {
            "event_type": "team.member",
            "session_id": "session-team",
            "event": {
                "type": "team.member.status_changed",
                "team_id": "team-1",
                "member_id": "researcher",
                "new_status": "working",
            },
        },
        EventType.TEAM_MEMBER,
        "web",
        id="team-member",
    ),
    pytest.param(
        "tui",
        {"mode": "team", "id": "swarmflow"},
        {
            "event_type": "workflow.updated",
            "session_id": "session-workflow",
            "workflow": {
                "id": "workflow-1",
                "status": "running",
                "summary": "",
            },
        },
        EventType.WORKFLOW_UPDATED,
        "tui",
        id="workflow-updated",
    ),
    pytest.param(
        "web",
        {"mode": "auto_harness", "id": "default"},
        {
            "event_type": "harness.stage_result",
            "stage": "implement",
            "status": "success",
            "error": None,
            "messages": ["implementation completed"],
            "metrics": {"tests_passed": 12},
        },
        None,
        "web",
        id="auto-harness-stage-result",
    ),
)

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "agent_ref", "payload", "expected_event_type", "renderer"),
    _MODE_EVENT_CASES,
)
async def test_mode_event_and_terminal_sentinel_cross_runtime_server_gateway_channel(
    channel_id: str,
    agent_ref: dict[str, str],
    payload: dict[str, Any],
    expected_event_type: EventType | None,
    renderer: str,
) -> None:
    """Keep mode events intact across Runtime, E2A, Gateway, and Channel layers."""
    request_id = f"request-{agent_ref['mode']}"
    session_id = str(payload.get("session_id") or "session-auto-harness")
    runtime_metadata = {
        "producer": agent_ref["mode"],
        "fan_out_targets": [agent_ref["id"]],
    }
    request = AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "exercise cross-layer protocol",
            "mode": agent_ref["mode"],
            "work_mode": "work",
        },
        is_stream=True,
        agent_ref=agent_ref,
    )
    manager = _RuntimeManager()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=_PlanController(),
    )
    runtime._prepare_chat_turn = AsyncMock(
        return_value=(
            agent_ref["mode"],
            None,
            _ProtocolAgent(payload=payload, metadata=runtime_metadata),
        )
    )
    runtime_events = [
        event async for event in runtime.stream(request, trigger_hook=False)
    ]
    await runtime.close()

    assert manager.foreground_calls == ["begin", "end"]
    assert len(runtime_events) == 2
    runtime_event, terminal_event = runtime_events
    assert runtime_event.session_id == session_id
    # The Runtime stamps the owning execution id onto events of work kinds
    # it tracks (Team modes here); untracked producers (auto_harness) keep
    # the payload untouched.
    expected_payload = dict(payload)
    execution_id = runtime_event.payload.get("execution_id")
    if execution_id is not None:
        expected_payload["execution_id"] = execution_id
    assert runtime_event.payload == expected_payload
    assert runtime_event.agent_ref == agent_ref
    assert runtime_event.metadata == runtime_metadata
    assert terminal_event.session_id == session_id
    assert terminal_event.payload is None
    assert terminal_event.agent_ref == agent_ref
    assert terminal_event.is_complete is True

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    websocket = _RecordingWebSocket()
    send_lock = asyncio.Lock()
    await server._send_runtime_event(
        websocket,
        runtime_event,
        send_lock,
        streaming=True,
        sequence=0,
    )
    await server._send_runtime_event(
        websocket,
        terminal_event,
        send_lock,
        streaming=True,
        sequence=1,
    )

    event_wire, terminal_wire = [json.loads(item) for item in websocket.sent]
    assert event_wire["request_id"] == request_id
    assert event_wire["channel"] == channel_id
    assert event_wire["response_kind"] == "e2a.chunk"
    assert event_wire["sequence"] == 0
    assert event_wire["is_final"] is False
    assert event_wire["agent_ref"] == agent_ref
    assert event_wire["metadata"] == runtime_metadata

    decoded_event = parse_agent_server_wire_chunk(event_wire)
    assert decoded_event.request_id == request_id
    assert decoded_event.channel_id == channel_id
    assert decoded_event.payload == expected_payload
    assert decoded_event.is_complete is False
    assert decoded_event.agent_ref == agent_ref
    assert decoded_event.metadata == runtime_metadata

    request_metadata = {"ws_id": "ws-1", "request_scope": agent_ref["mode"]}
    message = MessageHandler._chunk_to_message(
        decoded_event,
        session_id=session_id,
        metadata=request_metadata,
    )
    assert message.id == request_id
    assert message.type == "event"
    assert message.channel_id == channel_id
    assert message.session_id == session_id
    assert message.payload == expected_payload
    assert message.event_type == expected_event_type
    assert message.agent_ref == agent_ref
    assert message.metadata == {**request_metadata, **runtime_metadata}

    if renderer == "tui":
        channel_frame = json.loads(TuiChannel._serialize_frame(object(), message))
    else:
        channel = WebChannel.__new__(WebChannel)
        channel_frame = channel._serialize_frame(message)
    assert channel_frame["type"] == "event"
    assert channel_frame["event"] == payload["event_type"]
    expected_channel_payload = dict(expected_payload)
    expected_channel_payload.setdefault("session_id", session_id)
    expected_channel_payload["agent_ref"] = agent_ref
    assert channel_frame["payload"] == expected_channel_payload

    assert terminal_wire["request_id"] == request_id
    assert terminal_wire["response_kind"] == "e2a.complete"
    assert terminal_wire["sequence"] == 1
    assert terminal_wire["is_final"] is True
    decoded_terminal = parse_agent_server_wire_chunk(terminal_wire)
    assert decoded_terminal.agent_ref == agent_ref
    assert decoded_terminal.is_complete is True
    assert decoded_terminal.payload == {"is_complete": True}
    assert MessageHandler._is_terminal_stream_chunk(decoded_terminal) is True

# --- from test_obs_bucket_initialization.py ---

def s3_error(code):
    return S3Error(None, code, "test failure", "", "", "")

@pytest.mark.parametrize("operation", ["attachment", "delivery"])
@pytest.mark.parametrize(
    "state", ["exists", "missing", "owned_race", "exists_race", "still_missing", "denied", "network"]
)
def test_upload_initializes_bucket_without_hiding_storage_errors(tmp_path, operation, state):
    client = Mock()
    client.bucket_exists.return_value = state == "exists"
    client.put_object.side_effect = lambda bucket, key, reader, length, **kwargs: (
        reader.read(), SimpleNamespace(etag="etag")
    )[1]
    client.presigned_get_object.return_value = "http://obs/bucket/key"
    if state in {"owned_race", "exists_race", "still_missing"}:
        client.bucket_exists.side_effect = [False, state != "still_missing"]
        client.make_bucket.side_effect = s3_error(
            "BucketAlreadyOwnedByYou" if state == "owned_race" else "BucketAlreadyExists"
        )
    elif state == "denied":
        client.make_bucket.side_effect = s3_error("AccessDenied")
    elif state == "network":
        client.bucket_exists.side_effect = OSError("storage unavailable")
    source = tmp_path / "file.txt"
    source.write_bytes(b"data")
    store = AttachmentStore(client, "bucket", "http://obs")

    def upload():
        if operation == "attachment":
            return store.upload(b"data", "file.txt", "uploads/")
        return store.upload_file(source, "downloads/file.txt")

    if state in {"still_missing", "denied", "network"}:
        with pytest.raises((S3Error, OSError)):
            upload()
        client.put_object.assert_not_called()
    else:
        upload()
        client.put_object.assert_called_once()
        if state == "exists":
            client.make_bucket.assert_not_called()
        else:
            client.make_bucket.assert_called_once_with("bucket")

# --- from test_skilldev_schema_now_iso.py ---

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

def test_now_iso_format_and_no_deprecation_warning() -> None:
    before = datetime.now(timezone.utc).replace(microsecond=0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        stamp = _now_iso()
    after = datetime.now(timezone.utc).replace(microsecond=0)

    assert _ISO_Z.match(stamp), stamp
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)

    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= parsed <= after

# --- from test_stream_chunk_whitespace.py ---

@pytest.mark.parametrize(
    ("chunk_type", "content", "event_type"),
    [
        ("llm_output", " ", "chat.delta"),
        ("llm_output", "\n", "chat.delta"),
        ("content_chunk", "\n\n", "chat.delta"),
        ("llm_reasoning", " ", "chat.reasoning"),
    ],
)
def test_parse_stream_chunk_preserves_whitespace(
    chunk_type: str,
    content: str,
    event_type: str,
) -> None:
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        SimpleNamespace(type=chunk_type, payload={"content": content})
    )

    assert parsed == {"event_type": event_type, "content": content}

def test_stream_text_payload_skips_only_absent_or_empty_content() -> None:
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", None) is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", "") is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload(
        "chat.delta", " hello"
    ) == {"event_type": "chat.delta", "content": " hello"}

# --- from test_stream_event_rail_rewritten_args.py ---

class _StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)

def _ctx(model_args: dict, tool_args):
    tool_call = ToolCall(
        id="call-1", type="function", name="write", arguments=json.dumps(model_args)
    )
    return SimpleNamespace(
        session=_StreamSession(),
        inputs=ToolCallInputs(
            tool_call=tool_call, tool_name="write", tool_args=tool_args
        ),
        extra={},
        exception=None,
    )

@pytest.mark.asyncio
async def test_rewritten_arguments_reach_the_tool():
    """Arguments an approval rewrote are what the tool runs with."""
    model_args = {"path": "/etc/hosts", "content": "hi", "call_goal": "写入文件"}
    approved = {**model_args, "path": "/tmp/approved.txt"}
    ctx = _ctx(model_args, json.dumps(approved))

    await JiuSwarmStreamEventRail().before_tool_call(ctx)

    expected = {"path": "/tmp/approved.txt", "content": "hi"}
    assert json.loads(ctx.inputs.tool_args) == expected
    assert json.loads(ctx.inputs.tool_call.arguments) == expected

@pytest.mark.asyncio
async def test_call_goal_is_stripped_when_nothing_rewrote_the_arguments():
    """Without a rewrite the model's arguments are cleaned as before."""
    model_args = {"path": "a.txt", "content": "hi", "call_goal": "写入文件"}
    ctx = _ctx(model_args, json.dumps(model_args))

    await JiuSwarmStreamEventRail().before_tool_call(ctx)

    expected = {"path": "a.txt", "content": "hi"}
    assert json.loads(ctx.inputs.tool_args) == expected
    assert json.loads(ctx.inputs.tool_call.arguments) == expected

# --- from test_team_manager_skill_state.py ---

def test_configure_code_team_member_does_not_mount_coding_memory_workspace_node(monkeypatch, tmp_path):
    """code.team members keep Coding Memory out of the materialized workspace tree."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_code

    global_workspace = tmp_path / "global_agent_workspace"
    member_workspace = tmp_path / "member_workspace"
    parent_project = tmp_path / "project"
    global_workspace.mkdir()
    member_workspace.mkdir()
    parent_project.mkdir()

    monkeypatch.setattr(interface_code, "get_config", lambda: {"react": {}})
    monkeypatch.setattr(interface_code, "get_agent_workspace_dir", lambda: global_workspace)
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_refresh_multimodal_configs",
        lambda self, config: None,
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_create_model",
        lambda self, config: object(),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_create_sys_operation",
        lambda self: object(),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "build_code_tool_cards",
        lambda self, agent_id: [],
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_build_agent_rails",
        lambda self, react_config, config_base, mode: [],
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_build_configured_subagents",
        lambda self, model, react_config, config_base: ([], False),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_extract_enabled_mcp_server_entries",
        lambda self, config_base: [],
    )

    class Workspace:
        def __init__(self, root_path):
            self.root_path = str(root_path)
            self.directories = []

        def set_directory(self, directory):
            self.directories.append(directory)

    class AbilityManager:
        @staticmethod
        def list():
            return []

        @staticmethod
        def add(card):
            raise AssertionError("no tool cards should be added in this test")

    workspace = Workspace(member_workspace)
    agent = SimpleNamespace(
        card=SimpleNamespace(id="counter-1", name="Counter 1"),
        deep_config=SimpleNamespace(
            workspace=workspace,
            model=None,
            sys_operation=None,
            subagents=[],
            mcps=[],
        ),
        ability_manager=AbilityManager(),
        add_rail=lambda rail: None,
    )
    parent_agent = SimpleNamespace(
        _jiuwenswarm_code_project_dir=str(parent_project),
        deep_config=SimpleNamespace(workspace=SimpleNamespace(root_path=str(parent_project))),
    )

    interface_code.configure_code_team_member_agent(
        agent,
        parent_agent=parent_agent,
        member_name="counter-1",
        role="counter",
    )

    assert workspace.directories == []

# --- from test_ttse_background_drain.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "future", "handle"])
async def test_drain_waits_for_each_supported_task_shape(kind):
    proceed = asyncio.Event()
    if kind == "future":
        underlying = asyncio.get_running_loop().create_future()
    else:
        underlying = asyncio.create_task(proceed.wait())
    resource = BackgroundTask.from_asyncio_task(underlying, group="test") if kind == "handle" else underlying
    rail = SimpleNamespace(_bg_tasks={resource})
    draining = asyncio.create_task(_drain_ttse_rail_background_tasks(rail, timeout_secs=2))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not draining.done()
        assert not underlying.done()
    finally:
        if kind == "future":
            underlying.set_result(None)
        else:
            proceed.set()
        await asyncio.wait_for(draining, 3)
    assert rail._bg_tasks == set()

@pytest.mark.asyncio
async def test_drain_cancels_raw_task_after_timeout_and_keeps_it_until_exit():
    cancelled, proceed = asyncio.Event(), asyncio.Event()

    async def writer():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await proceed.wait()

    task = asyncio.create_task(writer())
    rail = SimpleNamespace(_bg_tasks={task})
    try:
        await _drain_ttse_rail_background_tasks(rail, timeout_secs=0.01)
        await asyncio.wait_for(cancelled.wait(), 2)
        assert task in rail._bg_tasks
        assert not task.done()
    finally:
        proceed.set()
        await asyncio.wait_for(task, 2)
    await _drain_ttse_rail_background_tasks(rail, timeout_secs=0.01)

# --- from test_worker_optional_team_patches.py ---

@pytest.mark.parametrize("failed_stage", ["guard", "descriptions"])
@pytest.mark.parametrize("error_type", [AttributeError, ImportError, OSError])
def test_optional_team_patch_failure_preserves_remaining_runtime_patches(
    monkeypatch, caplog, failed_stage, error_type,
):
    from jiuwenswarm import llm_sse_patch, openjiuwen_fs_rwlock_patch
    from jiuwenswarm.agents.harness.common.tools import bash_tool_safety
    from jiuwenswarm.common import config, llm_provider_compat_patch, native_tokenizer_cache
    from jiuwenswarm.common import team_workspace_read_guard
    from jiuwenswarm.common.auth import login_credentials

    monkeypatch.setattr(runtime_backend.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(runtime_backend.logger, "propagate", False)
    # Existing log filters can format and clear LogRecord.exc_info. Observe
    # the warning call before those handlers process the exception.
    warning = Mock(wraps=runtime_backend.logger.warning)
    monkeypatch.setattr(runtime_backend.logger, "warning", warning)
    calls = []

    def apply(stage):
        calls.append(stage)
        if stage == failed_stage:
            raise error_type("SDK Team interface unavailable")

    monkeypatch.setattr(native_tokenizer_cache, "install_native_tokenizer_cache", lambda: None)
    monkeypatch.setattr(bash_tool_safety, "install_shell_tool_safety_hooks", lambda: None)
    monkeypatch.setattr(openjiuwen_fs_rwlock_patch, "install_fs_rwlock_task_affinity_patch",
                        lambda: apply("fs_rwlock"))
    monkeypatch.setattr(llm_provider_compat_patch, "apply_provider_compat_patches", lambda: None)
    monkeypatch.setattr(team_workspace_read_guard, "install_team_workspace_read_guard", lambda: apply("guard"))
    monkeypatch.setattr(team_workspace_read_guard, "preload_bundled_team_descriptions",
                        lambda: apply("descriptions"))
    monkeypatch.setattr(login_credentials, "apply_login_credential_patch", lambda: apply("login"))
    monkeypatch.setattr(llm_sse_patch, "apply_openai_sse_invoke_patch", lambda: apply("sse"))
    monkeypatch.setattr(config, "get_config", lambda: {"channels": {"xiaoyi": {"mode": "xiaoyi_claw"}}})

    runtime_backend._apply_runtime_entry_patches()

    assert calls == ["fs_rwlock", "guard", "descriptions", "login", "sse"]
    warnings = [record for record in caplog.records if "TeamWorkspace" in record.message]
    assert len(warnings) == 1
    warning.assert_called_once()
    assert warning.call_args.kwargs["exc_info"] is True

