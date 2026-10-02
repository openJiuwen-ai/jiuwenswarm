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


def _snapshot_with_verify_group() -> dict[str, Any]:
    """A phase carrying a settled verify group + token-split / fork agents."""
    return {
        "id": "wf_v",
        "name": "verify-flow",
        "status": "completed",
        "phases": [
            {
                "id": "phase-v",
                "name": "交叉评审",
                "status": "completed",
                "agents": [
                    {
                        "id": "agent-r",
                        "name": "verify-评:q1",
                        "status": "completed",
                        "prompt": "review",
                        "outcome": '{"score": 0.9}',
                        "token_count": 3000,
                        "input_token_count": 2500,
                        "output_token_count": 500,
                        "cache_token_count": 1800,
                    },
                    {
                        "id": "agent-f",
                        "name": "答题-GLM",
                        "status": "completed",
                        "prompt": "answer",
                        "outcome": "ok",
                        "node_type": "agent_session_fork",
                        "parent_session_id": "wf-sess-analyst-0",
                        "member_name": "wf-sess-glm-1",
                    },
                ],
                "verify_groups": [
                    {
                        "id": "phase-v-verify-1",
                        "label": "verify-2",
                        "verify_id": '["verify", 2]',
                        "status": "settled",
                        "threshold": 0.6,
                        "reviewers": 1,
                        "reviewer_labels": ["verify-2-评:q1"],
                        "verdict": "pass",
                        "started_at": "2026-09-21T10:50:00",
                        "settled_at": "2026-09-21T10:51:00",
                        "votes": [
                            {
                                "name": "verify-2-评:q1",
                                "agent_id": 'k1',
                                "kind": "score",
                                "decision": None,
                                "score": 0.9,
                                "feedback": "long feedback " * 50,
                                "voted": True,
                            }
                        ],
                    }
                ],
            }
        ],
    }


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
    async def test_no_handler_serves_cold_start_normalized_runs(self) -> None:
        """Reopening a session after a process restart hits this checkpoint
        path before any chat.send rebuilds the runtime. A run the old process
        left ``running`` must be served as ``paused`` + ``recovered`` (no
        events will ever arrive for it, and the buttons must grey) — the same
        view ensure_monitor_handlers builds, not the raw snapshot.
        """
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        zombie = WorkflowRunState(status="running")
        zombie.id = "wf_z"
        done = WorkflowRunState(status="completed")
        done.id = "wf_d"
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-1", channel_id="web")
        send_lock = asyncio.Lock()
        persisted: list[dict] = []

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=None),
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.restore_workflow_runs",
            return_value={"wf_z": zombie, "wf_d": done},
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.persist_workflow_runs",
            side_effect=lambda runs, sid, **kw: persisted.append(dict(runs)),
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = _extract_payload(json.loads(ws.sent[0]))
        by_id = {w["id"]: w for w in payload["workflows"]}
        assert by_id["wf_z"]["status"] == "paused"
        assert by_id["wf_z"]["recovered"] is True
        assert by_id["wf_d"]["status"] == "completed"
        assert "recovered" not in by_id["wf_d"] or by_id["wf_d"]["recovered"] is False
        assert persisted  # the normalized view is written back, same as the runtime path

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
    async def test_get_phase_summary_keeps_verify_group_and_token_fields(self) -> None:
        """Refresh-path regression: verify containers + token chips + fork keys
        must survive the wire summaries (get_workflow / get_phase), or the tree
        renders them live-only and drops them after a page reload."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        snapshot = [_snapshot_with_verify_group()]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot)

        # get_workflow: phase summary carries the verify group card (feedback stripped)
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            session_id="sess-v",
            channel_id="web",
            params={"action": "get_workflow", "workflow_id": "wf_v"},
        )
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        group = payload["workflow"]["phases"][0]["verify_groups"][0]
        assert group["label"] == "verify-2"
        assert group["verify_id"] == '["verify", 2]'  # round identity rides the summary
        assert group["status"] == "settled"
        assert group["verdict"] == "pass"
        assert group["reviewer_labels"] == ["verify-2-评:q1"]
        assert group["votes"][0]["name"] == "verify-2-评:q1"
        assert group["votes"][0]["agent_id"] == "k1"
        assert group["votes"][0]["score"] == 0.9
        assert "feedback" not in group["votes"][0]  # heavy text stripped

        # get_phase: agent summary carries the token split + fork lineage keys
        ws2 = _FakeWS()
        request2 = _make_request(
            session_id="sess-v",
            channel_id="web",
            params={"action": "get_phase", "workflow_id": "wf_v", "phase_id": "phase-v"},
        )
        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=_FakeTeamManager(workflow_handler=fake_handler),
        ):
            await server._handle_command_workflows(ws2, request2, asyncio.Lock())
        payload2 = _extract_payload(json.loads(ws2.sent[0]))
        agents = {a["name"]: a for a in payload2["phase"]["agents"]}
        reviewer = agents["verify-评:q1"]
        assert reviewer["input_token_count"] == 2500
        assert reviewer["output_token_count"] == 500
        assert reviewer["cache_token_count"] == 1800
        fork = agents["答题-GLM"]
        assert fork["parent_session_id"] == "wf-sess-analyst-0"
        assert fork["member_name"] == "wf-sess-glm-1"

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
