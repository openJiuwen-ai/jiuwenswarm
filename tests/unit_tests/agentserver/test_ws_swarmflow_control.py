# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for swarmflow pause/resume/stop WS RPC handlers in AgentWebSocketServer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace  # noqa: F401  (kept for parity with sibling tests)
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


class _FakeWS:
    """Fake WebSocket that records sent messages."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeController:
    """Fake BackgroundTaskController recording pause/resume/stop calls."""

    def __init__(self, *, acted: bool = True) -> None:
        self.acted = acted
        self.calls: list[tuple[str, str | None]] = []

    async def pause(self, run_id: str | None = None) -> bool:
        self.calls.append(("pause", run_id))
        return self.acted

    async def resume(self, run_id: str | None = None) -> bool:
        self.calls.append(("resume", run_id))
        return self.acted

    async def stop(self, run_id: str) -> bool:
        self.calls.append(("stop", run_id))
        return self.acted


class _FakeWorkflowHandler:
    """Fake WorkflowMonitorHandler exposing only what the stop path touches."""

    def __init__(self, runs: dict[str, Any]) -> None:
        self._runs = runs
        self.persist_calls = 0

    def get_run_states(self) -> dict[str, Any]:
        return dict(self._runs)

    async def stop_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.status != "paused":
            return False
        run.finalize_if_running("stopped")
        self.persist_calls += 1
        return True


def _make_request(
    session_id: str = "sess-1",
    channel_id: str = "web",
    request_id: str = "req-1",
    req_method: ReqMethod = ReqMethod.SWARMFLOW_PAUSE,
    params: dict[str, Any] | None = None,
) -> AgentRequest:
    """Create a minimal AgentRequest for a swarmflow control RPC."""
    return AgentRequest(
        request_id=request_id,
        session_id=session_id,
        channel_id=channel_id,
        req_method=req_method,
        params=params or {},
    )


def _decode_response(ws: _FakeWS) -> dict[str, Any]:
    """Decode the single recorded WS message back into an AgentResponse dict."""
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary

    assert len(ws.sent) == 1
    resp = parse_agent_server_wire_unary(__import__("json").loads(ws.sent[0]))
    return {
        "ok": resp.ok,
        "payload": resp.payload or {},
        "request_id": resp.request_id,
        "channel_id": resp.channel_id,
    }


async def _run_handler(server: Any, ws: _FakeWS, request: AgentRequest) -> None:
    send_lock = asyncio.Lock()
    req_method = request.req_method
    if req_method == ReqMethod.SWARMFLOW_PAUSE:
        await server._handle_swarmflow_pause(ws, request, send_lock)
    elif req_method == ReqMethod.SWARMFLOW_RESUME:
        await server._handle_swarmflow_resume(ws, request, send_lock)
    elif req_method == ReqMethod.SWARMFLOW_STOP:
        await server._handle_swarmflow_stop(ws, request, send_lock)
    else:  # pragma: no cover - only reached on test misuse
        raise AssertionError(f"unexpected req_method: {req_method}")


class TestHandleSwarmflowControl:
    """Tests for _handle_swarmflow_pause/_resume/_stop methods."""

    async def _invoke(
        self,
        req_method: ReqMethod,
        controller: _FakeController | None = None,
        params: dict[str, Any] | None = None,
        session_id: str = "sess-1",
        team_manager: Any = None,
    ) -> tuple[dict[str, Any], _FakeController]:
        from unittest.mock import patch

        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id=session_id, req_method=req_method, params=params)
        fake_controller = controller or _FakeController()
        # Default: the team is awake and has no workflow handler registered, so
        # every action goes straight to the controller (the historical path).
        team_manager = team_manager or SimpleNamespace(
            has_stream_task=lambda sid: True,
            get_workflow_handler=lambda sid: None,
        )
        with patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.get_background_task_controller",
            return_value=fake_controller,
        ), patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=team_manager,
        ):
            await _run_handler(server, ws, request)
        return _decode_response(ws), fake_controller

    # -- happy paths ----------------------------------------------------

    async def test_pause_calls_controller_and_returns_ok(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_PAUSE, params={"run_id": "wf_1"}
        )
        assert controller.calls == [("pause", "wf_1")]
        assert resp["ok"] is True
        assert resp["payload"] == {"run_id": "wf_1", "status": "paused"}

    async def test_resume_calls_controller_and_returns_ok(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_RESUME, params={"run_id": "wf_1"}
        )
        assert controller.calls == [("resume", "wf_1")]
        assert resp["ok"] is True
        assert resp["payload"] == {"run_id": "wf_1", "status": "resumed"}

    async def test_stop_calls_controller_and_returns_ok(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_STOP, params={"run_id": "wf_1"}
        )
        assert controller.calls == [("stop", "wf_1")]
        assert resp["ok"] is True
        assert resp["payload"] == {"run_id": "wf_1", "status": "stopped"}

    async def test_accepts_workflow_run_id_alias(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_PAUSE, params={"workflow_run_id": "wf_9"}
        )
        assert controller.calls == [("pause", "wf_9")]
        assert resp["ok"] is True
        assert resp["payload"] == {"run_id": "wf_9", "status": "paused"}

    # -- error paths ----------------------------------------------------

    async def test_missing_run_id_returns_error(self) -> None:
        resp, controller = await self._invoke(ReqMethod.SWARMFLOW_PAUSE, params={})
        assert controller.calls == []
        assert resp["ok"] is False
        assert resp["payload"].get("error") == "run_id is required"

    async def test_blank_run_id_returns_error(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_RESUME, params={"run_id": "   "}
        )
        assert controller.calls == []
        assert resp["ok"] is False
        assert resp["payload"].get("error") == "run_id is required"

    async def test_stop_marks_paused_run_terminal_in_snapshot(self) -> None:
        """Stopping a paused run (no engine event) must mark the snapshot stopped."""
        from unittest.mock import patch

        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        run = WorkflowRunState(status="paused")
        run.id = "wf_1"
        handler = _FakeWorkflowHandler({"wf_1": run})
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.SWARMFLOW_STOP, params={"run_id": "wf_1"}
        )
        team_manager = SimpleNamespace(
            has_stream_task=lambda session_id: True,
            get_workflow_handler=lambda session_id: handler,
        )
        with patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.get_background_task_controller",
            return_value=_FakeController(acted=True),
        ), patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=team_manager,
        ):
            await _run_handler(server, ws, request)

        assert run.status == "stopped"
        assert handler.persist_calls == 1

    async def test_control_while_team_asleep_is_rejected(self) -> None:
        """Team asleep: no leader harness can host the run, so every control
        is refused without touching the controller (the tree-view buttons are
        greyed then; resume goes through the leader's ask_user).
        """
        asleep = SimpleNamespace(
            has_stream_task=lambda sid: False, get_workflow_handler=lambda sid: None,
        )
        for method in (ReqMethod.SWARMFLOW_PAUSE, ReqMethod.SWARMFLOW_RESUME, ReqMethod.SWARMFLOW_STOP):
            resp, ctl = await self._invoke(method, params={"run_id": "wf_1"}, team_manager=asleep)
            assert resp["ok"] is False
            assert resp["payload"].get("error") == "team is not running"
            assert ctl.calls == []

    async def test_controller_false_returns_not_found(self) -> None:
        resp, controller = await self._invoke(
            ReqMethod.SWARMFLOW_STOP,
            controller=_FakeController(acted=False),
            params={"run_id": "wf_missing"},
        )
        assert controller.calls == [("stop", "wf_missing")]
        assert resp["ok"] is False
        assert resp["payload"].get("error") == "workflow run not found"

    async def test_response_carries_request_id_and_channel(self) -> None:
        resp, _ = await self._invoke(
            ReqMethod.SWARMFLOW_PAUSE,
            params={"run_id": "wf_1"},
            session_id="sess-x",
        )
        assert resp["request_id"] == "req-1"
        assert resp["channel_id"] == "web"


class TestSwarmflowControlDispatch:
    """Structural test that the dispatch chain routes the new req_methods."""

    async def test_dispatch_has_three_swarmflow_branches(self) -> None:
        import inspect

        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        source = inspect.getsource(AgentWebSocketServer._handle_message)
        for method in (ReqMethod.SWARMFLOW_PAUSE, ReqMethod.SWARMFLOW_RESUME, ReqMethod.SWARMFLOW_STOP):
            assert f"ReqMethod.{method.name}" in source
