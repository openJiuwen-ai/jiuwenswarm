# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for forwarding Worker diagnosis events to the browser through the Gateway."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

_SESSION_ID = "session-1"


def _handler(sent: list[tuple[str, str, dict[str, Any]]], *, channel: bool = True) -> MessageHandler:
    handler = object.__new__(MessageHandler)

    async def send_session_event(session_id: str, event: str, payload: dict[str, Any]) -> None:
        sent.append((session_id, event, payload))

    web_channel = SimpleNamespace(
        send_session_event=send_session_event,
        schedule_trajectory_updates=lambda _updates: sent.append(("trace", "scheduled", {})),
    )
    handler._resolve_web_channel = lambda: web_channel if channel else None
    handler._stream_sessions = {}
    return handler


def _wire(event: str, payload: dict[str, Any], *, session_id: str | None = _SESSION_ID) -> dict[str, Any]:
    return build_server_push_wire(
        {
            "request_id": f"diagnosis:{_SESSION_ID}:dg_1:{event}:3",
            "channel_id": "web",
            "session_id": session_id,
            "payload": {**payload, "event_type": event},
            "is_complete": False,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["trajectory.diagnosis.progress", "trajectory.diagnosis.status"])
async def test_diagnosis_events_reach_the_session_without_the_event_type(event: str) -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []
    body = {"session_id": _SESSION_ID, "diagnosis_id": "dg_1", "seq": 3}

    await _handler(sent)._handle_agent_server_push(_wire(event, body))

    assert sent == [(_SESSION_ID, event, body)]


@pytest.mark.asyncio
async def test_session_id_falls_back_to_the_wire_envelope() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    await _handler(sent)._handle_agent_server_push(
        _wire("trajectory.diagnosis.status", {"diagnosis_id": "dg_1", "seq": 4}),
    )

    assert [(item[0], item[1]) for item in sent] == [(_SESSION_ID, "trajectory.diagnosis.status")]


@pytest.mark.asyncio
async def test_unrelated_events_are_not_taken_by_the_diagnosis_handler() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []
    handler = _handler(sent)
    chunk = SimpleNamespace(payload={"event_type": "trace.updated"})

    assert await handler._handle_diagnosis_push(chunk, _SESSION_ID) is False
    assert await handler._handle_diagnosis_push(SimpleNamespace(payload=None), _SESSION_ID) is False
    assert sent == []


@pytest.mark.asyncio
async def test_missing_web_channel_or_session_drops_the_event_quietly() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []
    chunk = SimpleNamespace(payload={"event_type": "trajectory.diagnosis.status", "diagnosis_id": "dg_1"})

    assert await _handler(sent, channel=False)._handle_diagnosis_push(chunk, _SESSION_ID) is True
    assert await _handler(sent)._handle_diagnosis_push(chunk, None) is True
    assert sent == []


@pytest.mark.asyncio
async def test_a_failing_browser_send_does_not_break_the_push_loop() -> None:
    handler = object.__new__(MessageHandler)

    async def send_session_event(session_id: str, event: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("socket closed")

    handler._resolve_web_channel = lambda: SimpleNamespace(send_session_event=send_session_event)
    chunk = SimpleNamespace(
        payload={"event_type": "trajectory.diagnosis.status", "session_id": _SESSION_ID, "seq": 1},
    )

    assert await handler._handle_diagnosis_push(chunk, None) is True
