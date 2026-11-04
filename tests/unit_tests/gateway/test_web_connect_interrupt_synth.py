# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Issue 5762 / issue 5448 regression: no synthetic processing_status on resume.

``chat.interrupt_result`` frames are followed by a synthesized
processing_status so frame-driven frontends stay in sync. pause was already
exempt (the server broadcasts the authoritative false after parking the
round); resume must be exempt too — AgentServer treats resume as a no-op
("send the next message to continue"), so an unconditional synthetic true
flips the frontend back to a spinner with no running round left to end it
(the double-pause scenario verified E2E as issue 5762 / residual gap B of
issue 5448).
"""

from __future__ import annotations

import time

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey

SESSION_ID = "sess-synth"


class _FakeWs:
    def __init__(self, ws_id: str) -> None:
        self._jiuwen_ws_id = ws_id
        self.closed = False


def _make_channel() -> WebChannel:
    channel = object.__new__(WebChannel)
    channel._session_busy = {}
    channel._clients_by_key = {}
    channel._ws_by_id = {}
    channel._send_queues = {}
    channel._a4p_rebound_ws = {}
    # Register one live client on the session bucket so send() reaches the
    # broadcast path instead of dropping the frame as an orphan.
    rk = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id=SESSION_ID,
    )
    channel._clients_by_key[rk] = [_FakeWs("ws-synth")]
    return channel


def _interrupt_result_msg(intent: str) -> Message:
    return Message(
        id=f"req-{intent}",
        type="event",
        channel_id="web",
        session_id=SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": True,
            "message": "任务已暂停",
        },
        event_type=EventType.CHAT_INTERRUPT_RESULT,
        metadata={},
    )


async def _capture_broadcast(channel: WebChannel) -> list[dict]:
    calls: list[dict] = []

    async def _fake_broadcast(frame, clients):
        calls.append(frame)

    channel._broadcast_to = _fake_broadcast  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["pause", "resume"])
async def test_pause_and_resume_do_not_synthesize_processing_status(intent) -> None:
    channel = _make_channel()
    calls = await _capture_broadcast(channel)

    await channel.send(_interrupt_result_msg(intent))

    synth = [c for c in calls if c.get("event") == "chat.processing_status"]
    assert synth == [], (
        "pause/resume must not synthesize processing_status: the server "
        "(pause) or the next message's fresh stream (resume) owns the "
        "authoritative state"
    )


@pytest.mark.asyncio
async def test_cancel_still_synthesizes_false() -> None:
    channel = _make_channel()
    calls = await _capture_broadcast(channel)

    await channel.send(_interrupt_result_msg("cancel"))

    synth = [c for c in calls if c.get("event") == "chat.processing_status"]
    assert len(synth) == 1
    assert synth[0]["payload"]["is_processing"] is False


@pytest.mark.asyncio
async def test_supplement_still_synthesizes_true() -> None:
    """The exemption covers pause/resume only: a supplement replaces the
    running task with a new one immediately, so the frontend must flip back
    to processing (review P3 on !9184/!9185)."""
    channel = _make_channel()
    calls = await _capture_broadcast(channel)

    await channel.send(_interrupt_result_msg("supplement"))

    synth = [c for c in calls if c.get("event") == "chat.processing_status"]
    assert len(synth) == 1
    assert synth[0]["payload"]["is_processing"] is True
