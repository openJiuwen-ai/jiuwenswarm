# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import (
    HeartbeatExecutionService,
    SessionRunAdmission,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    HeartbeatJob,
    HeartbeatSchedule,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import (
    HeartbeatRailRuntime,
    HeartbeatRuntimeUnavailableError,
)
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import (
    HEARTBEAT_TOOL_NAMES,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail
from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_manager import AgentManager


def test_heartbeat_injection_preserves_work_and_code_adapter_initializer_state() -> None:
    for adapter in (JiuWenSwarmDeepAdapter(), JiuwenSwarmCodeAdapter()):
        assert adapter._last_models_config_fingerprint is None
        assert adapter._last_reload_system_prompt_fingerprint is None
        assert adapter._registered_mcp_server_ids == set()
        assert adapter._registered_mcp_servers == {}
        assert adapter._session_selected_mcp == set()
        assert adapter._runtime_state_write_task is None


@pytest.mark.asyncio
async def test_code_runtime_config_accepts_load_aware_browser_prompt_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code requests must not call the removed channel API on the browser rail."""
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = SimpleNamespace(
        ability_manager=SimpleNamespace(add=lambda _card: None),
    )
    adapter._subagent_rail = BrowserTaskPromptRail()
    adapter._runtime_prompt_rail = None
    adapter._project_memory_rail = None
    adapter._agent_workspace_dir = "/tmp"

    async def async_noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(adapter, "_seed_runtime_cwd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "_write_runtime_state", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_update_rails_for_mode", async_noop)
    monkeypatch.setattr(adapter, "_set_user_interaction_enabled", async_noop)
    monkeypatch.setattr(adapter, "_update_tools_for_mode", async_noop)
    monkeypatch.setattr(adapter, "_update_session_tools", async_noop)
    monkeypatch.setattr(adapter, "_refresh_acp_runtime_tools", lambda *_args: None)
    monkeypatch.setattr(adapter, "_update_prompt_for_mode", lambda *_args: None)
    monkeypatch.setattr(adapter, "_register_shared_tool", lambda *_args: None)

    await adapter._update_runtime_config(
        JiuWenSwarmDeepAdapter._RuntimeConfig(
            session_id="code-heartbeat-session",
            mode="agent.code.normal",
            channel_id="web",
            workspace="/tmp",
        )
    )


@pytest.mark.parametrize(
    ("adapter_cls", "mode"),
    [
        (JiuWenSwarmDeepAdapter, "agent"),
        (JiuwenSwarmCodeAdapter, "code"),
    ],
)
def test_single_agent_adapters_mount_heartbeat_rail(
    monkeypatch: pytest.MonkeyPatch,
    adapter_cls: type[JiuWenSwarmDeepAdapter],
    mode: str,
) -> None:
    adapter = adapter_cls()
    heartbeat_service = object()
    adapter.set_heartbeat_service(heartbeat_service)
    declared_rails = []

    def instantiate_heartbeat_only(rail_infos, _config_base):
        declared_rails.extend(rail_infos)
        return [
            info.build_func(**info.params)
            for info in rail_infos
            if info.attr_name == "_heartbeat_rail"
        ]

    monkeypatch.setattr(
        adapter,
        "_instantiate_rails",
        instantiate_heartbeat_only,
    )

    rails = adapter._build_agent_rails({}, {"models": {}}, mode=mode)

    assert [info.attr_name for info in declared_rails].count("_heartbeat_rail") == 1
    assert len(rails) == 1
    assert isinstance(rails[0], HeartbeatRail)
    assert rails[0]._runtime._service is heartbeat_service

    registered_tools = {}

    class AbilityManager:
        @staticmethod
        def add_ability(card, tool) -> None:  # noqa: ANN001
            registered_tools[card.name] = tool

    rails[0].init(SimpleNamespace(ability_manager=AbilityManager()))

    assert set(registered_tools) == HEARTBEAT_TOOL_NAMES


def test_session_identity_uses_legacy_owner_and_rejects_spoofing() -> None:
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"user_id": "owner-1"},
    ):
        assert (
            HeartbeatRailRuntime._validated_session_user_id("session-1", "")
            == "owner-1"
        )
        with pytest.raises(PermissionError, match="another user"):
            HeartbeatRailRuntime._validated_session_user_id(
                "session-1", "intruder"
            )


def test_session_identity_keeps_empty_legacy_owner_session_scoped() -> None:
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={},
    ):
        assert HeartbeatRailRuntime._validated_session_user_id("session-1", "") == ""


async def test_user_waiter_has_priority_over_next_heartbeat() -> None:
    admission = SessionRunAdmission()
    assert await admission.try_begin_heartbeat("s1", "hb-1") is True
    user = asyncio.create_task(admission.begin_user("s1"))
    await asyncio.sleep(0)
    assert await admission.try_begin_heartbeat("s1", "hb-2") is False
    await admission.end_heartbeat("s1", "hb-1")
    await user
    assert await admission.try_begin_heartbeat("s1", "hb-2") is False
    await admission.end_user("s1")
    assert await admission.try_begin_heartbeat("s1", "hb-2") is True
    await admission.end_heartbeat("s1", "hb-2")


async def test_other_heartbeat_is_session_busy_but_current_run_is_excluded() -> None:
    admission = SessionRunAdmission()
    service = HeartbeatExecutionService(object(), admission)

    assert await admission.try_begin_heartbeat("s1", "run-a") is True
    assert service.is_session_busy("s1", exclude_run_id="run-a") is False
    assert service.is_session_busy("s1", exclude_run_id="run-b") is True
    await admission.end_heartbeat("s1", "run-a")


async def test_team_round_uses_same_atomic_heartbeat_admission() -> None:
    admission = SessionRunAdmission()
    service = HeartbeatExecutionService(object(), admission)

    await admission.begin_team_user("team-busy")
    assert service.is_session_busy("team-busy") is True
    assert await admission.try_begin_heartbeat("team-busy", "hb-race") is False
    await admission.end_user("team-busy")
    assert service.is_session_busy("team-busy") is False


def test_retain_agent_transfers_pin_to_current_facade() -> None:
    events: list[tuple[str, object]] = []

    class Manager:
        def pin_agent(self, agent) -> None:  # noqa: ANN001
            events.append(("pin", agent))

        def unpin_agent(self, agent) -> None:  # noqa: ANN001
            events.append(("unpin", agent))

    manager = Manager()
    runtime = HeartbeatRailRuntime.__new__(HeartbeatRailRuntime)
    runtime._server = SimpleNamespace(get_agent_manager=lambda: manager)
    runtime._pinned_agents = {}
    old_agent = object()
    current_agent = object()

    runtime.retain_agent("s1", old_agent)
    runtime.retain_agent("s1", current_agent)

    assert events == [
        ("pin", old_agent),
        ("pin", current_agent),
        ("unpin", old_agent),
    ]
    assert runtime._pinned_agents == {"s1": current_agent}


@pytest.mark.parametrize("running", [False, True])
async def test_session_delete_quiesce_breaks_real_manager_retention_cycle(
    tmp_path, running: bool
) -> None:
    manager = AgentManager()
    run_started = asyncio.Event()

    class Server:
        def get_agent_manager(self):
            return manager

        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            run_started.set()
            await asyncio.Event().wait()

    with patch(
        "jiuwenswarm.agents.harness.code.rails.heartbeat.runtime.get_heartbeat_jobs_path",
        return_value=tmp_path / "heartbeat_jobs.json",
    ):
        runtime = HeartbeatRailRuntime(Server())

    class Facade:
        live = True

        async def cleanup_session_runtime(self, session_id: str) -> bool:
            if await runtime.should_retain_session(session_id):
                return False
            self.live = False
            return True

        def has_session_runtime(self, session_id: str) -> bool:
            return self.live

    facade = Facade()
    manager.agents["web"] = {"agent\0\0": facade}
    job = await runtime.store.create_job(
        name="retain",
        channel_id="web",
        session_id="s1",
        prompt="continue",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="web_rpc",
        next_run_at=1.0,
        now=0.0,
    )
    runtime.retain_agent("s1", facade)

    with pytest.raises(RuntimeError, match="cleanup_session_runtime failed"):
        await manager.cleanup_session_runtime(channel_id="web", session_id="s1")

    if running:
        decision, claimed, _ = await runtime.store.claim_run(
            job.id,
            "run-delete",
            1.0,
            trigger="scheduler",
            reschedule=True,
            next_run_at_after_claim=121.0,
        )
        assert decision == "run"
        message = SimpleNamespace(
            channel_id="web",
            chat_id=None,
            req_method=None,
            params={},
            timestamp=1.0,
            metadata={},
            user_id="",
            agent_ref=None,
        )
        assert await runtime.execution.dispatch(
            claimed, "run-delete", message
        ) is True
        await run_started.wait()

    await runtime.begin_session_delete("s1")

    assert await runtime.should_retain_session("s1") is False
    assert runtime.execution.active_session_ids() == set()
    assert await manager.cleanup_session_runtime(
        channel_id="web", session_id="s1"
    ) is True
    await runtime.commit_session_delete("s1")

    current = await runtime.store.get_job(job.id)
    assert current is not None
    assert current.enabled is False
    assert current.status == "disabled"
    assert id(facade) not in manager._agent_pins


async def test_agentserver_retries_heartbeat_start_after_transient_failure() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.is_available = False
            self.starts = 0

        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("transient store failure")
            self.is_available = True

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._heartbeat_runtime = Runtime()

    assert await server._try_start_heartbeat_runtime() is False
    assert await server._try_start_heartbeat_runtime() is True
    assert server._heartbeat_runtime.starts == 2


async def test_agentserver_maps_runtime_not_ready_to_service_unavailable() -> None:
    class Runtime:
        async def handle_operation(self, *_args, **_kwargs):
            raise HeartbeatRuntimeUnavailableError(
                "heartbeat scheduler is not ready"
            )

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._heartbeat_runtime = Runtime()
    ws = WebSocket()
    request = AgentRequest(
        request_id="heartbeat-not-ready",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.HEARTBEAT_JOB,
        params={"action": "list", "data": {}},
    )

    await server._handle_heartbeat_job(ws, request, asyncio.Lock())

    response = parse_agent_server_wire_unary(ws.sent[-1])
    assert response.ok is False
    assert response.payload == {
        "error": "heartbeat scheduler is not ready",
        "code": "SERVICE_UNAVAILABLE",
    }


async def test_execution_uses_local_agentserver_and_exact_completion() -> None:
    completed: list[tuple[str, str, str]] = []

    class Server:
        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            assert request.req_method.value == "chat.send"
            assert request.session_id == "s1"
            assert request.metadata["automation"]["kind"] == "heartbeat"

    class Scheduler:
        async def on_run_finished(self, job_id, run_id, *, outcome, **kwargs):  # noqa: ANN001
            completed.append((job_id, run_id, outcome))
            return True

    service = HeartbeatExecutionService(Server(), SessionRunAdmission())
    service.set_scheduler(Scheduler())
    job = HeartbeatJob(
        id="job-1",
        name="follow up",
        enabled=True,
        channel_id="web",
        session_id="s1",
        prompt="continue",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        next_run_at=1.0,
    )
    message = SimpleNamespace(
        channel_id="web",
        chat_id=None,
        req_method=__import__(
            "jiuwenswarm.common.schema.message", fromlist=["ReqMethod"]
        ).ReqMethod.CHAT_SEND,
        params={"query": "continue"},
        timestamp=1.0,
        metadata={"automation": {"kind": "heartbeat"}},
        user_id="u1",
        agent_ref=None,
    )
    assert await service.dispatch(job, "run-1", message) is True
    await asyncio.gather(*service._tasks.values())
    assert completed == [("job-1", "run-1", "succeeded")]
    assert service.active_session_ids() == set()


async def test_execution_cancel_is_exact() -> None:
    started = asyncio.Event()

    class Server:
        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            started.set()
            await asyncio.Event().wait()

    class Scheduler:
        async def on_run_finished(self, *args, **kwargs):  # noqa: ANN001
            return True

    service = HeartbeatExecutionService(Server(), SessionRunAdmission())
    service.set_scheduler(Scheduler())
    job = HeartbeatJob(
        id="job-1",
        name="follow up",
        enabled=True,
        channel_id="web",
        session_id="s1",
        prompt="continue",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        next_run_at=1.0,
    )
    message = SimpleNamespace(
        channel_id="web", chat_id=None, req_method=None, params={}, timestamp=1.0,
        metadata={}, user_id="", agent_ref=None,
    )
    await service.dispatch(job, "run-1", message)
    await started.wait()
    assert await service.cancel("another-run") is False
    assert service.has_active_run("run-1") is True
    assert await service.cancel("run-1") is True
    assert service.has_active_run("run-1") is False


async def test_gateway_disconnect_does_not_abort_protected_heartbeat_session() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "heartbeat-session"
    adapter._instance = SimpleNamespace(abort=AsyncMock())

    await adapter.abort_on_gateway_disconnect(
        exclude_session_ids={"heartbeat-session"}
    )

    adapter._instance.abort.assert_not_awaited()


async def test_active_heartbeat_prevents_session_adapter_cleanup() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._heartbeat_service = SimpleNamespace(
        should_retain_session=AsyncMock(return_value=True)
    )
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "heartbeat-session"
    adapter.cleanup = AsyncMock()

    cleaned = await adapter.cleanup_session_adapter("heartbeat-session")

    assert cleaned is False
    adapter._heartbeat_service.should_retain_session.assert_awaited_once_with(
        "heartbeat-session"
    )
    adapter.cleanup.assert_not_awaited()


async def test_completion_hook_failure_does_not_escape_execution_task() -> None:
    class Server:
        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            return None

    class Scheduler:
        async def on_run_finished(self, *args, **kwargs):  # noqa: ANN001
            return True

    async def failing_hook(session_id: str) -> None:
        raise RuntimeError(f"release failed for {session_id}")

    service = HeartbeatExecutionService(Server(), SessionRunAdmission())
    service.set_scheduler(Scheduler())
    service.set_completion_hook(failing_hook)
    job = HeartbeatJob(
        id="job-hook",
        name="follow up",
        enabled=True,
        channel_id="web",
        session_id="s-hook",
        prompt="continue",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        next_run_at=1.0,
    )
    message = SimpleNamespace(
        channel_id="web", chat_id=None, req_method=None, params={}, timestamp=1.0,
        metadata={}, user_id="", agent_ref=None,
    )

    assert await service.dispatch(job, "run-hook", message) is True
    await asyncio.gather(*service._tasks.values())
    assert service.active_session_ids() == set()
