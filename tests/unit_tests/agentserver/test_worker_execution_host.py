"""The resident execution owner does not construct a transport server."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost
from jiuwenswarm.server.worker.management_adapters import LazyAdapterRegistry


async def test_finite_management_tasks_drain_and_late_notifications_are_rejected():
    import inspect

    host = object.__new__(WorkerExecutionHost)
    began, drained = asyncio.Event(), asyncio.Event()

    async def work():
        began.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()

    task = host.spawn_background(work(), name="test-management-refresh")
    await began.wait()
    await host._close_background_tasks()
    assert task.cancelled() and drained.is_set()
    assert host._owned_background_tasks == set()
    late = work()
    with pytest.raises(RuntimeError, match="stopping"):
        host.spawn_background(late, name="late")
    assert inspect.getcoroutinestate(late) == inspect.CORO_CLOSED
    assert host._schedule_acp_push({}) is None


def test_new_process_constructs_host_without_transport_or_management_providers(
    tmp_path,
):
    source = r"""
import importlib.abc
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

blocked = (
    "jiuwenswarm.server.agent_ws_server",
    "jiuwenswarm.server.runtime.gateway_adapter.config_adapter",
    "jiuwenswarm.server.runtime.gateway_adapter.project_adapter",
    "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter",
    "jiuwenswarm.server.runtime.video_task.server_adapter",
)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("eager optional import: " + fullname)
sys.meta_path.insert(0, Guard())
from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost
host = WorkerExecutionHost()
assert type(host).__name__ == "WorkerExecutionHost"
assert host._runtime.agent_manager is host._agent_manager
assert host._heartbeat_runtime.admission is host._runtime._admission_controller
assert host._adapter_registry.get("chat.send") is None
assert host._tokenizer_service_instance is None
assert host._personal_context_host_instance is None
assert host._jiuwenbox_runner_instance is None
assert callable(host.dispatch_parsed_request)
assert callable(host._start_asset_services)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_provisioner import SessionProvisionState
host._runtime.record_session_input_intent = AsyncMock(return_value="disabled")
host._runtime.start = AsyncMock()
host._runtime.prepare_session_switch = AsyncMock(return_value=SimpleNamespace(state=SessionProvisionState.COMMITTED))
host._runtime.commit_session_provision = AsyncMock(return_value=SimpleNamespace(
    session_id="existing-session", mode="agent", switched=True,
))
async def web_navigation():
    sink = SimpleNamespace(send=AsyncMock())
    for method in (ReqMethod.SESSION_SWITCH, ReqMethod.SESSION_INPUT_INTENT):
        request = AgentRequest(request_id=method.value, channel_id="web",
            session_id="existing-session", req_method=method,
            params={"session_id": "existing-session", "mode": "agent"})
        await host.dispatch_parsed_request(sink, request, asyncio.Lock())
    assert sink.send.await_count == 2
    host._runtime.record_session_input_intent.assert_awaited_once()
    host._runtime.prepare_session_switch.assert_awaited_once()
asyncio.run(web_navigation())
assert not any(name in sys.modules for name in blocked)
print("EXECUTION_HOST_BOUNDARY_OK")
"""
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(root),
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
        PYTHONUTF8="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXECUTION_HOST_BOUNDARY_OK" in result.stdout


def test_adapter_constructor_failure_is_retried_and_only_requested_provider_loaded(
    monkeypatch,
):
    from jiuwenswarm.server.worker import management_adapters

    calls = []
    adapter = object()

    def build(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError("injected configuration preparation failure")
        return adapter

    imports = []

    def load(name):
        imports.append(name)
        return SimpleNamespace(ConfigAdapter=build)

    monkeypatch.setattr(management_adapters, "import_module", load)
    callback = object()
    registry = LazyAdapterRegistry(SimpleNamespace(_reload_after_config_save=callback))
    assert registry.get("chat.send") is None
    assert registry.contains("config.get")
    assert imports == []
    with pytest.raises(OSError, match="injected configuration"):
        registry.get("config.get")
    assert registry.get("config.get") is adapter
    assert registry.get("config.set") is adapter
    assert imports == ["jiuwenswarm.server.runtime.gateway_adapter.config_adapter"] * 2
    assert calls == [{"on_config_saved": callback}] * 2


@pytest.mark.asyncio
async def test_execution_minimum_requires_observation_and_releases_startup_priority():
    host = object.__new__(WorkerExecutionHost)
    resumed = []
    observed = []
    token = object()
    host._execution_minimum_observed = False
    host._startup_preparation_tokens = {token}
    host._preparation_registry = SimpleNamespace(resume_background=resumed.append)
    host._on_execution_min_ready = lambda: observed.append(True)
    host.bind_event_sink(SimpleNamespace(send=AsyncMock()), asyncio.Lock())
    assert not observed
    host.observe_execution_minimum_ready()
    host.observe_execution_minimum_ready()
    assert observed == [True]
    assert resumed == [token]
    assert host._startup_preparation_tokens == set()


@pytest.mark.asyncio
async def test_host_unary_projects_real_runtime_events_to_event_sink():
    host = object.__new__(WorkerExecutionHost)
    request = AgentRequest(
        request_id="host-fixture",
        channel_id="web",
        session_id="session-host",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "fixture"},
    )
    event = RuntimeEvent(
        request_id=request.request_id,
        channel_id=request.channel_id,
        session_id=request.session_id,
        payload={"event_type": "chat.completed", "output": "fixture result"},
        is_complete=True,
    )
    host._runtime = SimpleNamespace(
        agent_manager=object(), invoke=AsyncMock(return_value=[event])
    )
    host._agent_manager = host._runtime.agent_manager
    host._open_session_message_resume = AsyncMock(return_value=None)
    host._finalize_session_message_resume = AsyncMock()
    sink = SimpleNamespace(send=AsyncMock())
    await host._handle_unary(sink, request, asyncio.Lock())
    host._runtime.invoke.assert_awaited_once()
    wire = json.loads(sink.send.call_args.args[0])
    assert "fixture result" in json.dumps(wire)
    host._finalize_session_message_resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_shot_shutdown_stops_scheduler_before_runtime_without_new_resources(
    monkeypatch,
):
    order = []
    host = object.__new__(WorkerExecutionHost)
    runtime = SimpleNamespace(closed=False)

    async def close_runtime():
        order.append("runtime")
        runtime.closed = True

    runtime.close = close_runtime
    host._runtime = runtime
    host._agent_manager = SimpleNamespace(
        unpin_agent=Mock(side_effect=lambda _: order.append("unpin"))
    )
    host._scheduler_agent = object()
    host._scheduler_service = SimpleNamespace(
        stop_scheduler=AsyncMock(side_effect=lambda: order.append("scheduler")),
    )
    host._heartbeat_runtime = SimpleNamespace(stop=AsyncMock())
    host._kv_cache_application_owner = SimpleNamespace(suspend=AsyncMock())
    host._tokenizer_warmup_tasks = set()
    host._runtime_services_started = True
    for name in (
        "_preparation_registry",
        "_proactive_engine",
        "_sandbox_start_task",
        "_asset_start_task",
        "_asset_publish_api",
        "_archive_service",
        "_session_message_service",
        "_checkpointer_warmup_task",
        "_mcp_prewarm_task",
        "_login_credential_refresh_task",
        "_image_modality_refresh_task",
        "_server",
        "_runtime_push_handler",
        "_jiuwenbox_runner_instance",
        "_personal_context_start_task",
        "_personal_context_host_instance",
    ):
        setattr(host, name, None)
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.runtime.marketplace.hub_catalog_cache",
        SimpleNamespace(close_hub_catalog_cache=AsyncMock()),
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.symphony.service",
        SimpleNamespace(
            get_swarm_symphony_service=lambda: SimpleNamespace(close=AsyncMock())
        ),
    )
    await host.stop()
    assert order == ["scheduler", "unpin", "runtime"]
    assert host._runtime is runtime
    assert not host._runtime_services_started
    assert host._jiuwenbox_runner_instance is None
    assert host._personal_context_host_instance is None
    with pytest.raises(RuntimeError, match="has been closed"):
        await host.start()


@pytest.mark.asyncio
async def test_team_terminal_and_default_transport_use_same_resident_push_owner(
    monkeypatch,
):
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.runtime.host_services import (
        install_runtime_push_handler,
        restore_runtime_push_handler,
    )
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    # A singleton lookup must never create a second, unbound Runtime.
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    monkeypatch.setattr(
        AgentWebSocketServer,
        "get_instance",
        Mock(side_effect=AssertionError("transport singleton requested")),
    )
    sink = AsyncMock(return_value=True)
    previous = install_runtime_push_handler(sink)
    try:
        event = {"event_type": "workflow.updated", "workflow": {"status": "completed"}}
        manager = object.__new__(TeamManager)
        await manager._push_event_without_waiter("session-team", event)
        message = {
            "session_id": "session-expert",
            "payload": {"event_type": "chat.final"},
        }
        assert await WebSocketGatewayPushTransport().send_push(message)
    finally:
        restore_runtime_push_handler(sink, previous)
    assert sink.await_count == 2
    assert sink.await_args_list[0].args[0]["session_id"] == "session-team"
    assert sink.await_args_list[1].args[0] is message
