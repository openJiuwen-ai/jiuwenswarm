# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-layer protocol contracts for non-single-agent runtime modes."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.server import agent_ws_server


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
    runtime.prepare_chat_turn = AsyncMock(
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
    assert runtime_event.payload == payload
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
    assert decoded_event.payload == payload
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
    assert message.payload == payload
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
    expected_channel_payload = dict(payload)
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
