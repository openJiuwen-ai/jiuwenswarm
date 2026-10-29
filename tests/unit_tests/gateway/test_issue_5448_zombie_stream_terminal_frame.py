# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Issue 5448 regression tests: the team round-authoritative false frame.

Mechanism under test:

1. A team pause (chat.cancel intent=pause) parks the AgentServer-side round
   but never cancels the Gateway stream consumer (message_handler interrupt
   branch forwards pause to the agent only). The agent-side event stream is
   cancelled without an is_complete terminal frame, so the Gateway consumer
   hangs on its queue forever — a zombie registration left in
   ``_stream_sessions`` with ``emit=True``.
2. When the team finally goes idle, the surviving PERSISTENT stream delivers
   the authoritative ``chat.processing_status {is_processing: false}``. The
   withholding branch used to drop it while sibling (zombie or PERSISTENT)
   registrations existed — the frontend spinner never stopped (issue 5448;
   the multi-round variant was verified E2E on develop as "gap A").

Fixed behaviour asserted here:

- A team PERSISTENT stream's false frame is round-authoritative (team.idle /
  team.completed / pause broadcast) and passes regardless of sibling
  registrations.
- A non-team stream's false frame is still withheld while a sibling chat
  stream is running: G1 lifts the withhold for team owners only.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

SESSION_ID = "sess-issue-5448"
TEAM_MODE = "team.work.normal"
NON_TEAM_MODE = "agent.work.normal"


def team_chat_send(rid: str, *, mode: str = TEAM_MODE) -> Message:
    return Message(
        id=rid, type="req", channel_id="web", session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_SEND, is_stream=True, timestamp=0, ok=True,
        params={"query": rid, "mode": mode},
        metadata={"ws_id": "ws-5448", "user_id": "u-5448"},
    )


def _false_frame(rid: str, *, terminal: bool = True) -> AgentResponseChunk:
    payload = {
        "event_type": "chat.processing_status",
        "session_id": SESSION_ID,
        "is_processing": False,
    }
    if terminal:
        payload["is_complete"] = True
    return AgentResponseChunk(request_id=rid, channel_id="web", payload=payload)


class HangingClient:
    """AgentServer stand-in reproducing the pause/completion asymmetry.

    Streams with a scripted frame list emit those frames and then hang —
    PERSISTENT team streams stay alive across rounds by design. Streams
    without a script hang immediately: the agent-side event stream was
    cancelled without a terminal frame (the pause asymmetry), so the
    Gateway consumer waits forever — a zombie registration.
    """

    def __init__(self):
        self.scripts: dict[str, list[AgentResponseChunk]] = {}
        self.started = asyncio.Queue()
        self.release = asyncio.Event()

    async def send_request_stream(self, env):
        rid = env.request_id
        await self.started.put(rid)
        for chunk in self.scripts.get(rid, []):
            yield chunk
        await self.release.wait()


@pytest.fixture
async def gateway(monkeypatch):
    monkeypatch.setattr(MessageHandler, "_instance", None)
    client = HangingClient()
    handler = MessageHandler(client)
    handler._sync_agentos_cron_jobs = AsyncMock()
    handler._broadcast_task_global_running = AsyncMock()
    handler._trigger_before_chat_request_hook = AsyncMock()
    await handler.start_forwarding()
    yield handler, client
    client.release.set()
    for task in list(handler._stream_tasks.values()):
        task.cancel()
    await asyncio.gather(*handler._stream_tasks.values(), return_exceptions=True)
    await handler.stop_forwarding()
    if handler._fire_and_forget_tasks:
        await asyncio.gather(*handler._fire_and_forget_tasks, return_exceptions=True)


async def _start_stream(
    handler: MessageHandler, rid: str, *, mode: str = TEAM_MODE,
) -> None:
    msg = team_chat_send(rid, mode=mode)
    env = handler.message_to_e2a(msg)
    await handler._start_stream_task(msg, env, rid)


async def _drain_frontend(handler: MessageHandler, window: float) -> list[dict]:
    """Collect every frontend frame published within ``window`` seconds."""
    frames: list[dict] = []
    try:
        while True:
            msg = await asyncio.wait_for(
                handler.consume_robot_messages(timeout=None), timeout=window,
            )
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            frames.append({
                "rid": msg.id,
                "event_type": payload.get("event_type", ""),
                "is_processing": payload.get("is_processing"),
            })
    except (asyncio.TimeoutError, TimeoutError):
        return frames


def _processing_false_frames(frames: list[dict]) -> list[dict]:
    return [
        f for f in frames
        if f["event_type"] == "chat.processing_status" and f["is_processing"] is False
    ]


@pytest.mark.asyncio
async def test_team_authoritative_false_passes_despite_zombie_streams(gateway):
    handler, client = gateway

    # Rounds parked by pause: two chat.send consumers whose agent-side stream
    # was cancelled without a terminal frame.
    await _start_stream(handler, "zombie-924")
    await _start_stream(handler, "zombie-960")
    assert await asyncio.wait_for(client.started.get(), 5) == "zombie-924"
    assert await asyncio.wait_for(client.started.get(), 5) == "zombie-960"

    # The pause asymmetry: nothing cancelled the Gateway consumers, so both
    # tasks are still hanging on their receive queue — zombie registrations
    # that the old withholding branch treated as "the session is still busy".
    assert not handler._stream_tasks["zombie-924"].done()
    assert not handler._stream_tasks["zombie-960"].done()

    # The surviving PERSISTENT stream delivers the authoritative false frame
    # once the team goes idle. The sibling predicate still reports the
    # zombies (G1 changes the caller, not this fact) — and yet the team
    # owner's false frame must reach the frontend regardless.
    client.scripts["persistent-995"] = [_false_frame("persistent-995")]
    await _start_stream(handler, "persistent-995")
    assert await asyncio.wait_for(client.started.get(), 5) == "persistent-995"
    assert handler._session_has_streams_blocking_processing_false(
        SESSION_ID, exclude_rid="persistent-995",
    )

    frames = await _drain_frontend(handler, 0.5)
    delivered = _processing_false_frames(frames)
    assert delivered and delivered[-1]["rid"] == "persistent-995"


@pytest.mark.asyncio
async def test_non_team_false_still_withheld_while_sibling_running(gateway):
    handler, client = gateway

    # A long-running non-team conversation stream, and a second stream whose
    # single request ends while the first one is still running.
    await _start_stream(handler, "sibling-agent-777", mode=NON_TEAM_MODE)
    assert await asyncio.wait_for(client.started.get(), 5) == "sibling-agent-777"
    client.scripts["owner-agent-778"] = [_false_frame("owner-agent-778")]
    await _start_stream(handler, "owner-agent-778", mode=NON_TEAM_MODE)
    assert await asyncio.wait_for(client.started.get(), 5) == "owner-agent-778"

    # G1 lifts the withhold for team owners only: a non-team stream's false
    # frame is the end of a single request and must still be withheld while
    # a sibling chat stream is running (auto-finalize stays with the last
    # stream to exit).
    frames = await _drain_frontend(handler, 0.5)
    assert _processing_false_frames(frames) == []


@pytest.mark.asyncio
async def test_team_single_work_false_without_terminal_still_withheld(gateway):
    handler, client = gateway

    # Round-authoritative terminals carry is_complete=True; a bare false is
    # one of several parallel owner works finishing (org root team) and must
    # keep waiting for its siblings even on a team stream. This guards the
    # narrowing of G1: team mode alone must not lift the withhold.
    await _start_stream(handler, "owner-work-1")
    assert await asyncio.wait_for(client.started.get(), 5) == "owner-work-1"
    client.scripts["owner-work-2"] = [_false_frame("owner-work-2", terminal=False)]
    await _start_stream(handler, "owner-work-2")
    assert await asyncio.wait_for(client.started.get(), 5) == "owner-work-2"

    frames = await _drain_frontend(handler, 0.5)
    assert _processing_false_frames(frames) == []


@pytest.mark.asyncio
async def test_team_second_round_false_passes_with_first_round_alive(gateway):
    handler, client = gateway

    # Round 1 completes: its authoritative false frame is delivered (no
    # sibling registration exists at that point) and its PERSISTENT stream
    # survives into round 2 by design.
    client.scripts["persistent-round1"] = [_false_frame("persistent-round1")]
    await _start_stream(handler, "persistent-round1")
    assert await asyncio.wait_for(client.started.get(), 5) == "persistent-round1"
    frames = await _drain_frontend(handler, 0.5)
    assert [f["rid"] for f in _processing_false_frames(frames)] == ["persistent-round1"]

    # Round 2 starts while round 1's stream is still registered. The old
    # withholding branch dropped round 2's false frame (gap A, verified E2E
    # on develop: agent _deliver fired, gateway never dispatched, frontend
    # never received it); it must now pass.
    client.scripts["persistent-round2"] = [_false_frame("persistent-round2")]
    await _start_stream(handler, "persistent-round2")
    assert await asyncio.wait_for(client.started.get(), 5) == "persistent-round2"
    frames = await _drain_frontend(handler, 0.5)
    assert [f["rid"] for f in _processing_false_frames(frames)] == ["persistent-round2"]

    # The round-1 PERSISTENT stream keeps living — survivorship is by design
    # and must not be disturbed by round 2's terminal signal.
    assert not handler._stream_tasks["persistent-round1"].done()


@pytest.mark.asyncio
async def test_pause_cancels_gateway_stream_consumers(gateway):
    handler, client = gateway
    handler._send_interrupt_to_agent = AsyncMock(
        return_value=SimpleNamespace(ok=True, request_id="pause-req-1")
    )

    # A running round plus a PERSISTENT survivor from an earlier round.
    await _start_stream(handler, "persistent-round1")
    await _start_stream(handler, "foreground-round2")
    assert await asyncio.wait_for(client.started.get(), 5) == "persistent-round1"
    assert await asyncio.wait_for(client.started.get(), 5) == "foreground-round2"

    # chat.cancel intent=pause walks the forward loop's pause branch. Hold
    # the task references first: once cancelled, the consumers pop themselves
    # out of _stream_tasks in their finally blocks.
    victim_tasks = {
        rid: handler._stream_tasks[rid]
        for rid in ("persistent-round1", "foreground-round2")
    }
    pause_msg = Message(
        id="pause-req-1", type="req", channel_id="web", session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_CANCEL, is_stream=False, timestamp=0, ok=True,
        params={"intent": "pause", "mode": TEAM_MODE},
        metadata={"ws_id": "ws-5448", "user_id": "u-5448"},
    )
    await handler.publish_user_messages(pause_msg)

    # The gateway consumers are cancelled — no zombie registration survives
    # pause (issue 5448's production trigger: the agent-side round is parked
    # and its event streams die without a wire terminal frame, so consumers
    # left alive would hang on their receive queues forever).
    async with asyncio.timeout(2):
        while not all(t.done() for t in victim_tasks.values()):
            await asyncio.sleep(0.01)
    assert SESSION_ID not in handler._stream_sessions.values()

    # The frontend sees the session-level false: the last cancelled consumer
    # finalizes itself (process_stream finally), so the spinner stops.
    frames = await _drain_frontend(handler, 0.5)
    assert _processing_false_frames(frames)


@pytest.mark.asyncio
async def test_pause_on_idle_session_broadcasts_false(gateway):
    """Double-pause on an idle session (issue 5762 / gap B).

    No stream is left to cancel and no cancelled consumer can finalize
    itself, so the explicit fallback must broadcast the session-level false
    — otherwise a resume-synthesized true strands the frontend spinner
    until the next terminal event.
    """
    handler, client = gateway
    handler._send_interrupt_to_agent = AsyncMock(
        return_value=SimpleNamespace(ok=True, request_id="pause-req-2")
    )

    pause_msg = Message(
        id="pause-req-2", type="req", channel_id="web", session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_CANCEL, is_stream=False, timestamp=0, ok=True,
        params={"intent": "pause", "mode": TEAM_MODE},
        metadata={"ws_id": "ws-5448", "user_id": "u-5448"},
    )
    await handler.publish_user_messages(pause_msg)

    frames = await _drain_frontend(handler, 0.5)
    assert _processing_false_frames(frames)


@pytest.mark.asyncio
async def test_pause_on_single_agent_session_keeps_streams(gateway):
    """Single-agent pause is a recoverable park, not a kill (review P2).

    The Deep adapter parks the round via StreamEventRail and the SAME stream
    keeps producing after resume, so the gateway consumer must survive the
    pause — cancelling it would drop the resumed round's output mid-flight.
    """
    handler, client = gateway
    handler._send_interrupt_to_agent = AsyncMock(
        return_value=SimpleNamespace(ok=True, request_id="pause-req-3")
    )

    await _start_stream(handler, "agent-round-1", mode=NON_TEAM_MODE)
    assert await asyncio.wait_for(client.started.get(), 5) == "agent-round-1"

    pause_msg = Message(
        id="pause-req-3", type="req", channel_id="web", session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_CANCEL, is_stream=False, timestamp=0, ok=True,
        params={"intent": "pause", "mode": NON_TEAM_MODE},
        metadata={"ws_id": "ws-5448", "user_id": "u-5448"},
    )
    await handler.publish_user_messages(pause_msg)

    # Let the fire-and-forget governance settle, then assert the consumer
    # survived: the stream must keep its gateway consumer for the resume.
    await asyncio.sleep(0.2)
    task = handler._stream_tasks.get("agent-round-1")
    assert task is not None and not task.done()

    # No session-level false either: the round is parked, not finished.
    frames = await _drain_frontend(handler, 0.3)
    assert not _processing_false_frames(frames)


@pytest.mark.asyncio
async def test_pause_governance_skipped_when_interrupt_rejected(gateway):
    """Governance must not cancel consumers before the pause is confirmed.

    If the AgentServer rejects or fails the interrupt the round is still
    producing on those streams; cancelling the gateway consumers would
    silently drop its output (the mirror image of issue 5448).
    """
    handler, client = gateway
    handler._send_interrupt_to_agent = AsyncMock(
        return_value=SimpleNamespace(ok=False, request_id="pause-req-4")
    )

    await _start_stream(handler, "team-round-1")
    assert await asyncio.wait_for(client.started.get(), 5) == "team-round-1"

    pause_msg = Message(
        id="pause-req-4", type="req", channel_id="web", session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_CANCEL, is_stream=False, timestamp=0, ok=True,
        params={"intent": "pause", "mode": TEAM_MODE},
        metadata={"ws_id": "ws-5448", "user_id": "u-5448"},
    )
    await handler.publish_user_messages(pause_msg)

    await asyncio.sleep(0.2)
    task = handler._stream_tasks.get("team-round-1")
    assert task is not None and not task.done()

    frames = await _drain_frontend(handler, 0.3)
    assert not _processing_false_frames(frames)
