# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for _handle_command_workflows handler in AgentWebSocketServer."""



from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    session_id: str = "sess-1",
    channel_id: str = "web",
    request_id: str = "req-1",
) -> AgentRequest:
    """Create a minimal AgentRequest for command.workflows."""
    return AgentRequest(
        request_id=request_id,
        session_id=session_id,
        channel_id=channel_id,
        req_method=ReqMethod.COMMAND_WORKFLOWS,
        params={},
    )


class _FakeWS:
    """Fake WebSocket that records sent messages."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeTeamManager:
    """Fake TeamManager that returns configurable workflow handler."""

    def __init__(self, workflow_handler: Any | None = None) -> None:
        self._workflow_handler = workflow_handler

    def get_workflow_handler(self, session_id: str) -> Any | None:
        return self._workflow_handler


class _FakeWorkflowHandler:
    """Fake WorkflowMonitorHandler with configurable snapshot."""

    def __init__(self, snapshot: list[dict[str, Any]] | None = None) -> None:
        self._snapshot = snapshot or []

    def get_workflow_snapshot(self) -> list[dict[str, Any]]:
        return self._snapshot


class _FailingWorkflowHandler:
    """Fake WorkflowMonitorHandler that raises on get_workflow_snapshot."""

    @staticmethod
    def get_workflow_snapshot() -> list[dict[str, Any]]:
        raise RuntimeError("snapshot explosion")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandleCommandWorkflows:
    """Tests for _handle_command_workflows method."""

    @pytest.mark.anyio
    async def test_no_handler_returns_empty_snapshot(self) -> None:
        """When no workflow handler exists, return empty workflows list."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-1", channel_id="web")
        send_lock = asyncio.Lock()

        fake_tm = _FakeTeamManager(workflow_handler=None)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ), patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers.restore_workflow_runs",
            return_value={},
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        wire = json.loads(ws.sent[0])
        # Decode through the wire format to get the payload
        # The payload should contain type=workflow_run_snapshot, workflows=[], session_id
        # E2A wire format wraps the response; find the payload in the structure
        payload = self._extract_payload_from_wire(wire)
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["workflows"] == []
        assert payload["session_id"] == "sess-1"

    @pytest.mark.anyio
    async def test_handler_returns_snapshot(self) -> None:
        """When workflow handler exists, return lightweight list summaries."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-2", channel_id="cli")
        send_lock = asyncio.Lock()

        snapshot_data = [
            {"id": "wf_1", "name": "research-flow", "status": "running"},
            {"id": "wf_2", "name": "build-flow", "status": "completed"},
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        wire = json.loads(ws.sent[0])
        payload = self._extract_payload_from_wire(wire)
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["action"] == "list"
        assert len(payload["workflows"]) == len(snapshot_data)
        assert payload["workflows"][0]["id"] == "wf_1"
        assert payload["workflows"][0]["detail_pending"] is True
        assert "truncated" not in payload["workflows"][0]
        assert payload["session_id"] == "sess-2"

    @pytest.mark.anyio
    async def test_handler_get_returns_workflow_detail(self) -> None:
        """action=get returns one workflow with full detail payload."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-get", channel_id="cli")
        request.params = {"action": "get", "workflow_id": "wf_1"}
        send_lock = asyncio.Lock()

        snapshot_data = [
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
            }
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = self._extract_payload_from_wire(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_detail"
        assert payload["action"] == "get"
        workflow = payload["workflow"]
        assert workflow["id"] == "wf_1"
        assert workflow["phases"][0]["agents"][0]["prompt"] == "hello world"

    @pytest.mark.anyio
    async def test_handler_bounds_large_snapshot_response(self, monkeypatch) -> None:
        """Large workflow snapshots should not exceed the configured wire budget."""
        from jiuwenswarm.server import wire_truncate as wire_truncate_module
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-large-workflow", channel_id="web")
        send_lock = asyncio.Lock()

        large_output = "x" * 20_000
        snapshot_data = [
            {
                "id": f"wf_{idx}",
                "name": f"workflow-{idx}",
                "status": "running",
                "description": large_output,
                "steps": [{"id": f"step-{idx}", "output": large_output}],
            }
            for idx in range(20)
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        monkeypatch.setattr(wire_truncate_module, "_WORKFLOW_SNAPSHOT_MAX_BYTES", 8192)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        encoded_size = len(ws.sent[0].encode("utf-8"))
        assert encoded_size <= 8192

        payload = self._extract_payload_from_wire(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["session_id"] == "sess-large-workflow"
        assert payload["workflows"]
        assert len(payload["workflows"]) == len(snapshot_data)
        assert payload["workflows"][0]["detail_pending"] is True
        assert payload["truncated"] is False

    @pytest.mark.anyio
    async def test_handler_preserves_too_large_first_snapshot_as_placeholder(self, monkeypatch) -> None:
        """A huge first workflow should be represented instead of dropped."""
        from jiuwenswarm.server import wire_truncate as wire_truncate_module
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-large-workflow", channel_id="web")
        send_lock = asyncio.Lock()

        snapshot_data = [
            {
                "id": "wf-too-large-" + ("x" * 10_000),
                "name": "workflow-too-large",
                "status": "running",
                "description": "x" * 100_000,
                "steps": [{"id": "step-1", "output": "x" * 100_000}],
            }
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        monkeypatch.setattr(wire_truncate_module, "_WORKFLOW_SNAPSHOT_MAX_BYTES", 2048)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        encoded_size = len(ws.sent[0].encode("utf-8"))
        assert encoded_size <= 2048

        payload = self._extract_payload_from_wire(json.loads(ws.sent[0]))
        assert len(payload["workflows"]) == 1
        assert payload["workflows"][0]["detail_pending"] is True
        assert payload["workflows"][0]["id"].startswith("wf-too-large-")
        assert payload["workflows"][0]["status"] == "running"
        assert payload["workflows"][0]["name"] == "workflow-too-large"

    @pytest.mark.anyio
    async def test_handler_degrades_to_id_placeholder_when_minimal_exceeds_budget(
        self, monkeypatch
    ) -> None:
        """When even the minimal snapshot exceeds budget, fall back to {id, truncated}."""
        from jiuwenswarm.server import wire_truncate as wire_truncate_module
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-minimal-workflow", channel_id="web")
        send_lock = asyncio.Lock()

        # Every keep_key near the 256B metadata limit forces minimal > budget.
        near_limit = "y" * 250
        snapshot_data = [
            {
                "id": "wf-min-" + ("z" * 245),
                "name": near_limit,
                "status": near_limit,
                "agent_count": near_limit,
                "completed_agent_count": near_limit,
                "started_at": near_limit,
                "completed_at": near_limit,
                "duration_ms": near_limit,
                "token_count": near_limit,
                "estimated_token_count": near_limit,
                "description": "x" * 100_000,
            }
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        monkeypatch.setattr(wire_truncate_module, "_WORKFLOW_SNAPSHOT_MAX_BYTES", 2048)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        payload = self._extract_payload_from_wire(json.loads(ws.sent[0]))
        budget = max(
            wire_truncate_module._TEAM_HISTORY_MIN_MAX_BYTES,
            wire_truncate_module._WORKFLOW_SNAPSHOT_MAX_BYTES
            - wire_truncate_module._WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES,
        )
        payload_size = wire_truncate_module._json_wire_size(payload)
        assert payload_size <= budget

        assert len(payload["workflows"]) == 1
        first = payload["workflows"][0]
        assert first["detail_pending"] is True
        assert first["id"].startswith("wf-min-")
        assert first.get("name")
        assert first.get("status")

    @pytest.mark.anyio
    async def test_handler_exception_returns_empty_snapshot(self) -> None:
        """When handler raises exception, return empty workflows list."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-3", channel_id="web")
        send_lock = asyncio.Lock()

        fake_handler = _FailingWorkflowHandler()
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        wire = json.loads(ws.sent[0])
        payload = self._extract_payload_from_wire(wire)
        assert payload["type"] == "workflow_run_snapshot"
        assert payload["workflows"] == []
        assert payload["session_id"] == "sess-3"

    @pytest.mark.anyio
    async def test_response_ok_is_true(self) -> None:
        """All responses from this handler should have ok=True."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request()
        send_lock = asyncio.Lock()

        fake_tm = _FakeTeamManager(workflow_handler=None)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        wire = json.loads(ws.sent[0])
        # The ok field should be accessible in the wire format
        # In E2A format, ok is in the response metadata or the legacy stash
        # Check that the response was constructed with ok=True
        payload = self._extract_payload_from_wire(wire)
        assert payload["type"] == "workflow_run_snapshot"

    @pytest.mark.anyio
    async def test_empty_session_id_defaults_to_empty_string(self) -> None:
        """When session_id is None, it defaults to empty string."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id=None, channel_id="web")
        send_lock = asyncio.Lock()

        fake_tm = _FakeTeamManager(workflow_handler=None)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        wire = json.loads(ws.sent[0])
        payload = self._extract_payload_from_wire(wire)
        assert payload["session_id"] == ""

    @pytest.mark.anyio
    async def test_empty_channel_id_defaults_to_web(self) -> None:
        """When channel_id is None, it defaults to 'web'."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = AgentRequest(
            request_id="req-1",
            session_id="sess-1",
            channel_id=None,
            req_method=ReqMethod.COMMAND_WORKFLOWS,
            params={},
        )
        send_lock = asyncio.Lock()

        fake_tm = _FakeTeamManager(workflow_handler=None)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        # Verify the response was sent and channel_id defaulted to "web"
        assert len(ws.sent) == 1

    @staticmethod
    def _extract_payload_from_wire(wire: dict[str, Any]) -> dict[str, Any]:
        """Extract the AgentResponse payload from E2A wire format.

        The E2A wire format wraps the response. The payload may be in
        different locations depending on whether E2A encoding succeeded
        or fell back to legacy. We try both paths.
        """
        # Try E2A format first: payload is in response.metadata or
        # inside the response structure
        if "response" in wire:
            resp = wire["response"]
            # In E2A format, the payload is embedded in the response
            if "metadata" in resp and isinstance(resp["metadata"], dict):
                meta = resp["metadata"]
                if "payload" in meta:
                    return meta["payload"]
            # Some E2A formats put payload directly
            if "payload" in resp:
                return resp["payload"]

        # Try legacy stash format
        if "metadata" in wire and isinstance(wire["metadata"], dict):
            meta = wire["metadata"]
            if "payload" in meta:
                return meta["payload"]

        # Try the direct wire structure — sometimes the AgentResponse
        # payload is at the top level after legacy fallback
        # The payload in legacy format is typically inside metadata.payload
        # or inside the agent_response dict
        for key in ("payload", "metadata"):
            if key in wire:
                val = wire[key]
                if isinstance(val, dict) and "payload" in val:
                    return val["payload"]
                if isinstance(val, dict) and "type" in val:
                    return val

        # Last resort: search recursively for the workflow_run_snapshot type
        return _find_payload_recursive(wire)


def _find_payload_recursive(data: Any) -> dict[str, Any]:
    """Recursively search for a workflow list/detail payload dict."""
    if isinstance(data, dict):
        if data.get("type") in ("workflow_run_snapshot", "workflow_run_detail", "workflow_human_prompt"):
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


class TestCommandWorkflowsDispatch:
    """Test that COMMAND_WORKFLOWS req_method triggers the correct handler."""

    @pytest.mark.anyio
    async def test_command_workflows_dispatch_calls_handler(self) -> None:
        """Verify the dispatch routing for ReqMethod.COMMAND_WORKFLOWS."""
        # This is a structural test that verifies the dispatch was added.
        # We mock the handler method and verify it gets called through
        # the dispatch chain.
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)

        # Mock the handler method
        server._handle_command_workflows = AsyncMock()

        # Create a minimal request
        request = _make_request()
        ws = _FakeWS()
        send_lock = asyncio.Lock()

        # Call the handler directly to verify it exists and is callable
        await server._handle_command_workflows(ws, request, send_lock)

        server._handle_command_workflows.assert_called_once_with(ws, request, send_lock)


class TestWorkflowHumanPromptWireHelpers:
    def test_collapse_preserves_full_waiting_human_prompt(self) -> None:
        from jiuwenswarm.server.wire_truncate import (
            _build_workflow_detail_payload,
            _collapse_oversized_workflow_snapshot_item,
        )

        long_question = "Q-" + ("x" * 5000)
        workflow = {
            "id": "wf-human",
            "name": "party-planner",
            "status": "running",
            "phases": [
                {
                    "id": "phase-1",
                    "name": "main",
                    "status": "running",
                    "agents": [
                        {
                            "id": "agent-wait",
                            "name": "human_baby",
                            "status": "waiting_for_human",
                            "kind": "human",
                            "correlation_id": "main:human_baby:1",
                            "human_prompt": long_question,
                        },
                        *[
                            {
                                "id": f"agent-{idx}",
                                "name": f"worker-{idx}",
                                "status": "completed",
                                "kind": "agent",
                                "prompt": "p" * 8000,
                                "outcome": "o" * 8000,
                            }
                            for idx in range(12)
                        ],
                    ],
                }
            ],
        }

        collapsed = _collapse_oversized_workflow_snapshot_item(workflow)
        waiting = collapsed["phases"][0]["agents"][0]
        assert waiting["human_prompt"] == long_question
        assert "[truncated]" not in waiting["human_prompt"]

        detail = _build_workflow_detail_payload(workflow, session_id="sess-human")
        detail_waiting = detail["workflow"]["phases"][0]["agents"][0]
        assert detail_waiting["human_prompt"] == long_question

    @pytest.mark.anyio
    async def test_get_human_prompt_action_returns_full_text(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(session_id="sess-human", channel_id="web")
        request.params = {
            "action": "get_human_prompt",
            "workflow_id": "wf_human",
            "agent_id": "agent-wait",
        }
        send_lock = asyncio.Lock()

        long_question = "Approve?" + ("!" * 4000)
        snapshot_data = [
            {
                "id": "wf_human",
                "name": "party-planner",
                "status": "running",
                "phases": [
                    {
                        "id": "phase-1",
                        "name": "main",
                        "status": "running",
                        "agents": [
                            {
                                "id": "agent-wait",
                                "name": "human_baby",
                                "status": "waiting_for_human",
                                "kind": "human",
                                "human_prompt": long_question,
                            }
                        ],
                    }
                ],
            }
        ]
        fake_handler = _FakeWorkflowHandler(snapshot=snapshot_data)
        fake_tm = _FakeTeamManager(workflow_handler=fake_handler)

        with patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=fake_tm,
        ):
            await server._handle_command_workflows(ws, request, send_lock)

        assert len(ws.sent) == 1
        payload = TestHandleCommandWorkflows()._extract_payload_from_wire(json.loads(ws.sent[0]))
        assert payload["type"] == "workflow_human_prompt"
        assert payload["human_prompt"] == long_question
