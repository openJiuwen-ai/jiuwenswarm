from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_delete import TeamDeleteResult
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.asyncio
async def test_team_delete_handler_only_maps_runtime_result(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    runtime = SimpleNamespace(
        delete_team=AsyncMock(
            return_value=TeamDeleteResult(
                ok=False,
                team_name="team-a",
                session_ids=("s1",),
                failed_session_ids=("s2",),
                deleted=True,
                recovery_required=True,
                error_code="DELETE_FAILED",
                error_message="partial",
            )
        )
    )
    server._runtime = runtime
    server._execution_runtime = lambda: runtime
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.encode_agent_response_for_wire",
        lambda response, **_kwargs: response,
    )
    send_wire = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.send_wire_payload",
        send_wire,
    )
    request = AgentRequest(
        request_id="delete",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "team-a"},
    )
    ws = object()
    await server._handle_team_delete(ws, request, asyncio.Lock())

    runtime.delete_team.assert_awaited_once_with(team_name="team-a", channel_id="web")
    response = send_wire.await_args.args[1]
    assert response.ok is False
    assert response.payload == {
        "team_name": "team-a",
        "session_ids": ["s1"],
        "failed_session_ids": ["s2"],
        "deleted": True,
        "recovery_required": True,
        "error": "partial",
        "code": "DELETE_FAILED",
    }


@pytest.mark.asyncio
async def test_plain_disconnect_does_not_run_delete_participant():
    participant = SimpleNamespace(
        name="derived-resource",
        before_delete=AsyncMock(),
        release_resources=AsyncMock(),
        delete_failed=AsyncMock(),
        delete_committed=AsyncMock(),
    )
    registry = RuntimeParticipantRegistry()
    registry.replace_session_participants(activity=(), delete=(participant,))
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(),
        cleanup=AsyncMock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        participant_registry=registry,
    )

    await runtime.close()

    participant.before_delete.assert_not_awaited()
    participant.release_resources.assert_not_awaited()
    participant.delete_failed.assert_not_awaited()
    participant.delete_committed.assert_not_awaited()
