# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the enterprise HTTP chat stream at its real SSE frame boundary."""

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.transports.http_sink import SSESink
from jiuwenswarm.server.transports import sse_chat_deltas


def _open(monkeypatch, edition="enterprise", mode="agent.work.normal", **params):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    sink = SSESink()
    sink.configure_chat(AgentRequest(request_id="r", channel_id="web", params={"mode": mode, **params}))
    return sink


async def _send(sink, seq, kind, text, *, terminal=False, metadata=None, **fields):
    chunk = AgentResponseChunk(
        request_id="r", channel_id="web", is_complete=terminal, metadata=metadata,
        payload={"event_type": kind, "content": text, **fields},
    )
    assert await sink.send_chunk(chunk, sequence=seq)


def _decode(frame):
    return json.loads(frame.removeprefix("data: "))


@pytest.mark.asyncio
async def test_burst_preserves_text_and_order_across_kind_source_and_tool(monkeypatch):
    # A long window makes boundaries, rather than test machine speed, control flushing.
    monkeypatch.setattr(sse_chat_deltas, "_FLUSH_SECONDS", 60)
    sink = _open(monkeypatch)
    seq = 0
    expected = []
    for kind, source, text in [("chat.reasoning", "a", "想\n法"), ("chat.delta", "a", "正文。"),
                              ("chat.delta", "b", "乙🙂段"), ("chat.delta", "a", "回来！")]:
        for char in text:
            await _send(sink, seq, kind, char, stream_source_id=source)
            seq += 1
        expected.extend([(kind, source, text[0]), (kind, source, text[1:])])
    await _send(sink, seq, "chat.tool_call", "tool")
    await _send(sink, seq + 1, "chat.final", "完", terminal=True)
    frames = [_decode(frame) async for frame in sink.frames()]
    payloads = [frame["payload"] for frame in frames]
    assert [(p["event_type"], p.get("stream_source_id"), p["content"]) for p in payloads] == [
        *expected, ("chat.tool_call", None, "tool"), ("chat.final", None, "完"),
    ]
    assert [frame["sequence"] for frame in frames] == list(range(len(frames)))
    assert frames[-1]["is_complete"]


@pytest.mark.asyncio
async def test_interleaved_push_and_control_frames_use_wire_order(monkeypatch):
    monkeypatch.setattr(sse_chat_deltas, "_FLUSH_SECONDS", 60)
    sink = _open(monkeypatch)
    for seq, char in enumerate("abcd"):
        await _send(sink, seq, "chat.delta", char)
    await _send(sink, 4, "plan.mode_exited", "")
    sink.offer_keepalive()
    await _send(sink, 0, "chat.file", "report")
    await _send(sink, 4, "chat.delta", "e")
    await _send(sink, 5, "chat.final", "done", terminal=True)

    raw = [frame async for frame in sink.frames()]
    assert raw[3] == ": keepalive\n\n"
    chunks = [_decode(frame) for frame in raw if frame.startswith("data: ")]
    assert [(f["sequence"], f["payload"]["event_type"], f["payload"]["content"]) for f in chunks] == [
        (0, "chat.delta", "a"), (1, "chat.delta", "bcd"), (2, "plan.mode_exited", ""),
        (3, "chat.file", "report"), (4, "chat.delta", "e"), (5, "chat.final", "done"),
    ]


@pytest.mark.asyncio
async def test_idle_stream_flushes_without_next_model_event(monkeypatch):
    sink = _open(monkeypatch)
    frames = sink.frames()
    await _send(sink, 0, "chat.reasoning", "first")
    assert _decode(await anext(frames))["payload"]["content"] == "first"
    await _send(sink, 1, "chat.reasoning", "tail")
    await _send(sink, 2, "chat.reasoning", "\n")
    next_frame = await asyncio.wait_for(anext(frames), timeout=1)
    assert _decode(next_frame)["payload"]["content"] == "tail\n"
    await sink.close()
    assert [frame async for frame in frames] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("edition,params", [
    ("personal", {}), ("enterprise", {"mode": "team.work.normal"}), ("enterprise", {"team": True}),
])
async def test_personal_and_team_frames_keep_their_granularity(monkeypatch, edition, params):
    sink = _open(monkeypatch, edition, **params)
    sequences = [2, 3, 4, 0, 4, 5]
    for index, seq in enumerate(sequences):
        await _send(sink, seq, "chat.reasoning", str(index))
    await sink.close()
    frames = [_decode(frame) async for frame in sink.frames()]
    assert [f["payload"]["content"] for f in frames] == list("012345")
    assert [f["sequence"] for f in frames] == sequences


@pytest.mark.asyncio
async def test_cursor_phase_and_failure_frames_are_individually_recoverable(monkeypatch):
    sink = _open(monkeypatch)
    for seq in range(3):
        await _send(sink, seq, "chat.delta", str(seq), metadata={"runtime_event": {"sequence": seq}})
    for seq in range(3, 6):
        await _send(sink, seq, "chat.delta", str(seq), output_phase_id="p", output_order={"sequence": seq})
    for seq in range(6, 10):
        await _send(sink, seq, "chat.delta", str(seq))
    await _send(sink, 10, "chat.error", "failed", terminal=True)
    frames = [_decode(frame) async for frame in sink.frames()]
    assert [f["metadata"]["runtime_event"]["sequence"] for f in frames[:3]] == list(range(3))
    assert [f["payload"]["output_order"]["sequence"] for f in frames[3:6]] == list(range(3, 6))
    assert [f["payload"]["content"] for f in frames] == [*list("0123456"), "789", "failed"]


@pytest.mark.asyncio
async def test_live_output_phase_keeps_first_order_and_flushes_on_gaps_and_phase_changes(monkeypatch):
    monkeypatch.setattr(sse_chat_deltas, "_FLUSH_SECONDS", 60)
    sink = _open(monkeypatch)
    orders = [("a", 7), ("a", 8), ("a", 9), ("a", 12), ("a", 13), ("b", 14), ("b", 15), ("b", 16)]
    for index, (phase, sequence) in enumerate(orders):
        await _send(sink, index, "chat.delta", str(index), output_phase_id=phase, timestamp=sequence * 10,
                    output_order={"request_id": "r", "sequence": sequence})
    await sink.close()
    payloads = [_decode(frame)["payload"] async for frame in sink.frames()]
    assert [(p["content"], p["output_phase_id"], p["output_order"]["sequence"], p["timestamp"]) for p in payloads] == [
        ("0", "a", 7, 70), ("12", "a", 8, 80), ("3", "a", 12, 120), ("4", "a", 13, 130),
        ("5", "b", 14, 140), ("67", "b", 15, 150),
    ]


@pytest.mark.asyncio
async def test_size_bound_and_full_queue_at_close(monkeypatch):
    monkeypatch.setattr(sse_chat_deltas, "_MAX_CHARS", 3)
    sink = _open(monkeypatch)
    for seq in range(10):
        await _send(sink, seq, "chat.delta", str(seq))
    # Fill exactly the remaining slots, putting the terminal frame in the last one.
    remaining = sink._queue.maxsize - sink.pending_frame_count()
    for offset in range(remaining):
        await _send(sink, 10 + offset, "chat.tool_update", str(offset), terminal=offset == remaining - 1)
    frames = [_decode(frame) async for frame in sink.frames()]
    parts = [f["payload"]["content"] for f in frames if f["payload"]["event_type"] == "chat.delta"]
    assert "".join(parts) == "0123456789"
    assert all(len(part) <= 3 for part in parts)
    assert [f["payload"]["content"] for f in frames[-remaining:]] == list(map(str, range(remaining)))
    assert frames[-1]["is_complete"]
