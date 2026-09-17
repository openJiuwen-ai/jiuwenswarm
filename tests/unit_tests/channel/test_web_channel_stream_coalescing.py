# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebChannel stream coalescing keeps content exact and barriers ordered."""

import asyncio
import json
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel


class _FakeWebSocket:
    closed = False

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)


def _channel() -> WebChannel:
    return WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())


def _frame(event: str, content: str, **metadata: Any) -> dict[str, Any]:
    """Build a coalescible stream frame as a dict (what _enqueue_send now stores)."""
    return {
        "type": "event",
        "event": event,
        "payload": {**metadata, "content": content},
    }


def _decoded(frame: str) -> dict[str, Any]:
    """Decode a wire frame the writer serialized before ws.send."""
    return json.loads(frame)


def test_coalesce_merges_contiguous_delta_with_identical_metadata() -> None:
    channel = _channel()
    queue = asyncio.Queue()
    queue.put_nowait(_frame("chat.delta", "B", session_id="s1"))
    queue.put_nowait(_frame("chat.final", "AB", session_id="s1"))

    frames = channel._coalesce(
        _frame("chat.delta", "A", session_id="s1"),
        queue,
    )

    assert frames[0]["payload"]["content"] == "AB"
    assert frames[1]["event"] == "chat.final"


def test_coalesce_preserves_whitespace_exactly() -> None:
    channel = _channel()
    queue = asyncio.Queue()
    queue.put_nowait(_frame("chat.delta", " ", session_id="s1"))
    queue.put_nowait(_frame("chat.delta", "\n", session_id="s1"))

    frames = channel._coalesce(
        _frame("chat.delta", "A", session_id="s1"),
        queue,
    )

    assert frames[0]["payload"]["content"] == "A \n"


@pytest.mark.parametrize(
    ("first_metadata", "second_metadata"),
    [
        ({"session_id": "s1"}, {"session_id": "s2"}),
        (
            {"session_id": "s1", "member_name": "planner"},
            {"session_id": "s1", "member_name": "coder"},
        ),
        (
            {"session_id": "s1", "request_id": "r1"},
            {"session_id": "s1", "request_id": "r2"},
        ),
    ],
)
def test_coalesce_does_not_merge_different_stream_identity(
    first_metadata: dict[str, str],
    second_metadata: dict[str, str],
) -> None:
    channel = _channel()
    queue = asyncio.Queue()
    second = _frame("chat.delta", "B", **second_metadata)
    queue.put_nowait(second)

    first = _frame("chat.delta", "A", **first_metadata)
    frames = channel._coalesce(first, queue)

    assert frames == [first, second]


def test_coalesce_stops_at_reasoning_barrier() -> None:
    channel = _channel()
    queue = asyncio.Queue()
    reasoning = _frame("chat.reasoning", "think", session_id="s1")
    later_delta = _frame("chat.delta", "B", session_id="s1")
    queue.put_nowait(reasoning)
    queue.put_nowait(later_delta)

    first = _frame("chat.delta", "A", session_id="s1")
    frames = channel._coalesce(first, queue)

    assert frames == [first, reasoning]
    assert queue.get_nowait() == later_delta


def test_coalesce_keeps_malformed_first_frame_and_queue_untouched() -> None:
    channel = _channel()
    queue = asyncio.Queue()
    queue.put_nowait(_frame("chat.delta", "B", session_id="s1"))

    assert channel._coalesce("not-json", queue) == ["not-json"]
    assert queue.qsize() == 1


def test_coalesce_limits_each_batch_to_32_frames() -> None:
    channel = _channel()
    queue = asyncio.Queue()
    for _ in range(40):
        queue.put_nowait(_frame("chat.delta", "x", session_id="s1"))

    frames = channel._coalesce(
        _frame("chat.delta", "x", session_id="s1"),
        queue,
    )

    assert frames[0]["payload"]["content"] == "x" * 32
    assert queue.qsize() == 9


@pytest.mark.asyncio
async def test_writer_flushes_coalesced_content_before_sentinel() -> None:
    channel = _channel()
    ws = _FakeWebSocket()
    queue = asyncio.Queue()
    channel._send_queues["ws-1"] = queue
    queue.put_nowait(_frame("chat.delta", "A", session_id="s1"))
    queue.put_nowait(_frame("chat.delta", "B", session_id="s1"))
    queue.put_nowait(None)

    await asyncio.wait_for(channel._writer_loop(ws, "ws-1"), timeout=0.2)

    assert len(ws.sent) == 1
    assert _decoded(ws.sent[0])["payload"]["content"] == "AB"


@pytest.mark.asyncio
async def test_tui_writer_sends_all_frames_before_sentinel() -> None:
    channel = TuiChannel()
    ws = _FakeWebSocket()
    queue = asyncio.Queue()
    channel._send_queues["ws-1"] = queue
    queue.put_nowait("first")
    queue.put_nowait("second")
    queue.put_nowait(None)

    await asyncio.wait_for(channel._writer_loop(ws, "ws-1"), timeout=0.2)

    assert ws.sent == ["first", "second"]
