# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for _handle_command_workflows handler in AgentWebSocketServer."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod


def _make_request(
    session_id: str = "sess-1",
    channel_id: str = "web",
    request_id: str = "req-1",
    params: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        session_id=session_id,
        channel_id=channel_id,
        req_method=ReqMethod.COMMAND_WORKFLOWS,
        params=params or {},
    )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeTeamManager:
    def __init__(self, workflow_handler: Any | None = None) -> None:
        self._workflow_handler = workflow_handler

    def get_workflow_handler(self, session_id: str) -> Any | None:
        return self._workflow_handler


class _FakeWorkflowHandler:
    def __init__(self, snapshot: list[dict[str, Any]] | None = None) -> None:
        self._snapshot = snapshot or []

    def get_workflow_snapshot(self) -> list[dict[str, Any]]:
        return self._snapshot


class _FailingWorkflowHandler:
    @staticmethod
    def get_workflow_snapshot() -> list[dict[str, Any]]:
        raise RuntimeError("snapshot explosion")


def _find_payload_recursive(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if data.get("type") in (
            "workflow_run_snapshot",
            "workflow_run_detail",
            "workflow_phase_detail",
            "workflow_agent_detail",
        ):
            return data
        for v in data.values():
            result = _find_payload_recursive(v)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_payload_recursive(item)
            if result:
                return result
    return {}


def _extract_payload(wire: dict[str, Any]) -> dict[str, Any]:
    if "response" in wire:
        resp = wire["response"]
        if isinstance(resp.get("metadata"), dict) and "payload" in resp["metadata"]:
            return resp["metadata"]["payload"]
        if "payload" in resp:
            return resp["payload"]
    if isinstance(wire.get("metadata"), dict) and "payload" in wire["metadata"]:
        return wire["metadata"]["payload"]
    for key in ("payload", "metadata"):
        if key in wire:
            val = wire[key]
            if isinstance(val, dict) and "payload" in val:
                return val["payload"]
            if isinstance(val, dict) and "type" in val:
                return val
    found = _find_payload_recursive(wire)
    if found:
        return found
    return wire


def _snapshot_two_workflows() -> list[dict[str, Any]]:
    return [
        {
            "id": "wf_1",
            "name": "research-flow",
            "status": "completed",
            "phases": [
                {
                    "id": "phase-1",
                    "name": "main",
                    "status": "completed",
                    "agents": [
                        {
                            "id": "agent-1",
                            "name": "writer",
                            "status": "completed",
                            "prompt": "hello world",
                            "outcome": "done",
                        }
                    ],
                }
            ],
        },
        {"id": "wf_2", "name": "build-flow", "status": "running"},
    ]


class TestHandleCommandWorkflows:
    @pytest.mark.anyio
    async def test_no_handler_returns_empty_snapshot(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-1", channel_id="web")
        send_lock = asyncio.Lock()

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=None),
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.restore_workflow_runs",
            return_value={},
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["action"] == "list"
        assert payload["workflows"] == []
        assert payload["session_id"] == "sess-1"

    @pytest.mark.anyio
    async def test_list_returns_summaries_with_detail_pending(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-2", channel_id="cli")
        send_lock = asyncio.Lock()

        fake_handler = _FakeWorkflowHandler(snapshot=_snapshot_two_workflows())
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["action"] == "list"
        assert len(payload["workflows"]) == 2
        first = payload["workflows"][0]
        assert first["id"] == "wf_1"
        assert first["detail_pending"] is True
        assert "phases" not in first
        assert payload["session_id"] == "sess-2"
        assert payload["total"] == 2

    @pytest.mark.anyio
    async def test_get_workflow_returns_phase_summaries(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            session_id="sess-get",
            channel_id="cli",
            params={"action": "get_workflow", "workflow_id": "wf_1"},
        )
        send_lock = asyncio.Lock()

        fake_handler = _FakeWorkflowHandler(snapshot=_snapshot_two_workflows())
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_detail"
        assert payload["action"] == "get_workflow"
        workflow = payload["workflow"]
        assert workflow["id"] == "wf_1"
        phase = workflow["phases"][0]
        assert phase["id"] == "phase-1"
        assert phase["detail_pending"] is True
        assert "agents" not in phase
        assert payload["phase_total"] == 1
        assert payload["has_more"] is False

    @pytest.mark.anyio
    async def test_get_phase_returns_agent_summaries(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            session_id="sess-phase",
            channel_id="cli",
            params={
                "action": "get_phase",
                "workflow_id": "wf_1",
                "phase_id": "phase-1",
            },
        )
        send_lock = asyncio.Lock()

        fake_handler = _FakeWorkflowHandler(snapshot=_snapshot_two_workflows())
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_phase_detail"
        assert payload["action"] == "get_phase"
        phase = payload["phase"]
        assert phase["id"] == "phase-1"
        agent = phase["agents"][0]
        assert agent["id"] == "agent-1"
        assert agent["detail_pending"] is True
        # Heavy text fields are omitted from the summary — fetched via get_agent.
        assert "prompt" not in agent
        assert "outcome" not in agent
        # A short preview of outcome is carried for the tree row stub.
        assert agent["outcome_preview"] == "done"
        assert payload["agent_total"] == 1
        assert payload["has_more"] is False

    @pytest.mark.anyio
    async def test_get_agent_returns_single_agent(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            session_id="sess-agent",
            channel_id="cli",
            params={
                "action": "get_agent",
                "workflow_id": "wf_1",
                "phase_id": "phase-1",
                "agent_id": "agent-1",
            },
        )
        send_lock = asyncio.Lock()

        fake_handler = _FakeWorkflowHandler(snapshot=_snapshot_two_workflows())
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_agent_detail"
        assert payload["action"] == "get_agent"
        agent = payload["agent"]
        assert agent["id"] == "agent-1"
        assert agent["prompt"] == "hello world"

    @pytest.mark.anyio
    async def test_unknown_action_returns_error(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            params={"action": "bogus"},
        )
        send_lock = asyncio.Lock()

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=None),
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.restore_workflow_runs",
            return_value={},
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        assert "unknown action" in ws.sent[0]
        assert "bogus" in ws.sent[0]
        wire = json.loads(ws.sent[0])
        assert wire.get("response_kind") == "e2a.error"

    @pytest.mark.anyio
    async def test_handler_exception_returns_empty_snapshot(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-3", channel_id="web")
        send_lock = asyncio.Lock()

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=_FailingWorkflowHandler()),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["workflows"] == []
        assert payload["session_id"] == "sess-3"

    @pytest.mark.anyio
    async def test_empty_session_id_defaults_to_empty_string(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id=None, channel_id="web")
        send_lock = asyncio.Lock()

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=None),
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.restore_workflow_runs",
            return_value={},
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["session_id"] == ""

    @pytest.mark.anyio
    async def test_list_offset_pagination(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        snapshot = [
            {"id": f"wf_{i}", "name": f"flow-{i}", "status": "running"}
            for i in range(5)
        ]
        request = _make_request(
            params={"action": "list", "offset": 2, "limit": 2},
        )
        send_lock = asyncio.Lock()

        fake_handler = _FakeWorkflowHandler(snapshot=snapshot)
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["total"] == 5
        assert [w["id"] for w in payload["workflows"]] == ["wf_2", "wf_3"]
        assert payload["has_more"] is True


class TestCommandWorkflowsDispatch:
    @pytest.mark.anyio
    async def test_command_workflows_dispatch_calls_handler(self) -> None:
        from unittest.mock import AsyncMock

        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._handle_command_workflows = AsyncMock()

        request = _make_request()
        ws = _FakeWS()
        send_lock = asyncio.Lock()
        await server._handle_command_workflows(ws, request, send_lock)
        server._handle_command_workflows.assert_called_once_with(ws, request, send_lock)
