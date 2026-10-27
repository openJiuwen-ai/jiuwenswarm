# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MoA progress remains a transient structured event throughout transport."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_to_agent_chunk,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def test_moa_notice_does_not_confirm_user_turn() -> None:
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.runtime.service import AgentRuntime

    event = RuntimeEvent(
        request_id="request-A", channel_id="web", session_id="session-A",
        payload={"event_type": "chat.moa_status", "phase": "reference", "status": "started"},
    )
    assert not AgentRuntime._event_confirms_user_turn(event)
    event.payload = {"event_type": "runtime.accepted"}
    assert AgentRuntime._event_confirms_user_turn(event)


@pytest.mark.parametrize("phase", ["reference", "verification"])
@pytest.mark.parametrize("status", ["started", "completed", "failed", "cancelled"])
def test_moa_status_survives_wire_and_web_payload(phase: str, status: str) -> None:
    fields = {
        "event_type": "chat.moa_status",
        "operation_id": "operation-A",
        "phase": phase,
        "status": status,
        "execution_id": "execution-A",
    }
    chunk = AgentResponseChunk(
        request_id="request-A", channel_id="web", payload=fields, is_complete=False,
    )
    restored = e2a_response_to_agent_chunk(
        e2a_response_from_agent_chunk(chunk, response_id="request-A", sequence=1),
    )
    message = Message(
        id=restored.request_id, type="event", channel_id="web", session_id="session-A",
        params={}, timestamp=1, ok=True, payload=restored.payload,
    )

    payload = WebChannel._build_event_payload(message, "chat.moa_status")

    assert payload == {**fields, "session_id": "session-A", "request_id": "request-A"}


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_moa_status_is_forwarded_without_entering_history(monkeypatch, as_dict: bool) -> None:
    progress = [
        {"event_type": "chat.moa_status", "operation_id": "reference-A", "phase": "reference", "status": "started"},
        {"event_type": "chat.moa_status", "operation_id": "reference-A", "phase": "reference", "status": "completed"},
        {"event_type": "chat.moa_status", "operation_id": "verify-A", "phase": "verification", "status": "started"},
        {"event_type": "chat.moa_status", "operation_id": "verify-A", "phase": "verification", "status": "completed"},
    ]

    async def stream_impl(*_args, **_kwargs):
        for payload in [*progress, {"event_type": "chat.final", "content": "answer"}]:
            if as_dict:
                yield dict(payload)
            else:
                yield AgentResponseChunk(
                    request_id="request-A", channel_id="tui", payload=dict(payload), is_complete=False,
                )

    recorded = []
    monkeypatch.setattr(
        interface_module.JiuWenSwarm, "_ensure_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(process_message_stream_impl=stream_impl),
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: recorded.append(record))
    request = AgentRequest(
        request_id="request-A", channel_id="tui", session_id="session-A",
        params={"query": "hello", "mode": "agent"}, is_stream=True,
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    assert [chunk.payload for chunk in chunks if chunk.payload.get("event_type") == "chat.moa_status"] == progress
    assert not any(record.get("event_type") == "chat.moa_status" for record in recorded)
    assert any(record.get("event_type") == "chat.final" and record.get("content") == "answer" for record in recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reference", "verification"])
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_control_resume_forwards_moa_status_without_persisting_it(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    terminal: str,
) -> None:
    progress = [
        {"event_type": "chat.moa_status", "operation_id": "resume-operation", "phase": phase, "status": "started"},
        {"event_type": "chat.moa_status", "operation_id": "resume-operation", "phase": phase, "status": terminal},
    ]

    async def stream_impl(_request, _inputs):
        for payload in [
            progress[0],
            {"event_type": "chat.delta", "content": "resumed answer"},
            progress[1],
            {"event_type": "chat.final", "content": ""},
        ]:
            yield AgentResponseChunk(
                request_id="resume-request", channel_id="web", payload=payload, is_complete=False,
            )

    swarm = interface_module.JiuWenSwarm()
    recorded = []
    monkeypatch.setattr(swarm, "_ensure_adapter", lambda mode: SimpleNamespace(process_message_stream_impl=stream_impl))
    monkeypatch.setattr(swarm, "_session_manager", SimpleNamespace(get_session_id=lambda sid: sid))
    monkeypatch.setattr(swarm, "_build_inputs", lambda request: ({}, "disabled", None))
    monkeypatch.setattr(swarm, "reconcile_session_mcp", AsyncMock())
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: recorded.append(record))
    request = AgentRequest(
        request_id="resume-request", channel_id="web", session_id="session-A", is_stream=True,
        params={
            "source": "ask_user_interrupt", "mode": "agent", "request_id": "original-request",
            "answers": [{"question": "Which option?", "selected_options": ["A"]}],
        },
    )

    chunks = [chunk async for chunk in swarm.deliver_control_input(request)]

    assert [chunk.payload for chunk in chunks if chunk.payload.get("event_type") == "chat.moa_status"] == progress
    assert not any(record.get("event_type") == "chat.moa_status" for record in recorded)
    assert any(record.get("event_type") == "chat.final" and record.get("content") == "resumed answer" for record in recorded)
    assert any(record.get("event_type") == "chat.ask_user_answer" for record in recorded)
