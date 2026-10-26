# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BaseWsChannel outbound queue: capped, drops instead of piling up memory."""

from __future__ import annotations

import asyncio
import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from weakref import ref

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.routing import base_ws_channel


def _channel() -> WebChannel:
    return WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())


@pytest.mark.asyncio
async def test_register_ws_bounds_the_outbound_queue():
    from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey

    channel = _channel()
    ws = SimpleNamespace(closed=False)

    await channel.register_ws(
        ws,
        RoutingKey(
            user_id="u",
            channel_id="web",
            app_id="default",
            agent_ref=AgentRef(mode="agent", id="default"),
            session_id="s1",
        ),
    )

    queue = channel._send_queues[ws._jiuwen_ws_id]
    assert queue.maxsize == base_ws_channel._SEND_QUEUE_MAX_FRAMES


def test_enqueue_send_drops_frames_once_the_queue_is_full(monkeypatch):
    channel = _channel()
    ws = SimpleNamespace(closed=False, _jiuwen_ws_id="ws-1")
    channel._send_queues["ws-1"] = asyncio.Queue(maxsize=2)
    queue = channel._send_queues["ws-1"]
    dequeue = Mock(wraps=queue.get_nowait)
    monkeypatch.setattr(queue, "get_nowait", dequeue)

    for i in range(5):
        channel._enqueue_send(ws, f"f{i}")

    dequeue.assert_not_called()
    assert queue.qsize() == 2
    assert [queue.get_nowait(), queue.get_nowait()] == ["f0", "f1"]


@pytest.mark.parametrize("frame", ["frame", {"type": "event", "event": "chat.moa_status", "payload": {}}])
def test_enqueue_send_ignores_closed_ws(frame):
    channel = _channel()
    ws = SimpleNamespace(closed=True, _jiuwen_ws_id="ws-1")
    channel._send_queues["ws-1"] = asyncio.Queue()

    channel._enqueue_send(ws, frame)

    assert channel._send_queues["ws-1"].qsize() == 0
    assert not channel._queued_moa_notices


@pytest.mark.parametrize("event", ["chat.delta", "chat.final", "execution.error", "chat.processing_status"])
def test_congestion_drops_moa_notices_before_normal_frames(event, monkeypatch):
    channel = _channel()
    ws = SimpleNamespace(closed=False, _jiuwen_ws_id="ws-1")
    queue = asyncio.Queue(maxsize=3)
    channel._send_queues["ws-1"] = queue
    first = {"type": "event", "event": "chat.delta", "payload": {"content": "first"}}
    hint = {"type": "event", "event": "chat.moa_status", "payload": {"status": "started"}}
    second = {"type": "event", "event": "chat.delta", "payload": {"content": "second"}}
    incoming = {"type": "event", "event": event, "payload": {"content": "last"}}
    for frame in (first, hint, second):
        channel._enqueue_send(ws, frame)
    assert channel._queued_moa_notices[queue] == 1
    channel._enqueue_send(ws, incoming)
    assert queue not in channel._queued_moa_notices
    dequeue = Mock(wraps=queue.get_nowait)
    monkeypatch.setattr(queue, "get_nowait", dequeue)
    channel._enqueue_send(ws, "dropped after hints were cleared")
    dequeue.assert_not_called()
    assert [queue.get_nowait() for _ in range(queue.qsize())] == [first, second, incoming]


def test_moa_notice_cannot_displace_a_full_queue():
    channel = _channel()
    ws = SimpleNamespace(closed=False, _jiuwen_ws_id="ws-1")
    queue = asyncio.Queue(maxsize=1)
    channel._send_queues["ws-1"] = queue
    queue.put_nowait("ordinary output")
    channel._enqueue_send(ws, {"type": "event", "event": "chat.moa_status", "payload": {}})
    assert not channel._queued_moa_notices
    assert queue.qsize() == 1
    assert queue.get_nowait() == "ordinary output"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_deltas", [False, True])
async def test_consumed_moa_notices_do_not_trigger_future_scans(monkeypatch, after_deltas):
    channel = _channel()
    ws = SimpleNamespace(closed=False, _jiuwen_ws_id="ws-1", send=AsyncMock())
    queue = asyncio.Queue(maxsize=5)
    channel._send_queues["ws-1"] = queue
    hints = [
        {"type": "event", "event": "chat.moa_status", "payload": {"status": status}}
        for status in ("started", "completed")
    ]
    if after_deltas:
        for text in ("first", "second"):
            channel._enqueue_send(ws, {"type": "event", "event": "chat.delta", "payload": {"content": text}})
    for hint in hints:
        channel._enqueue_send(ws, hint)
    assert channel._queued_moa_notices[queue] == 2
    queue.put_nowait(None)

    await asyncio.wait_for(channel._writer_loop(ws, "ws-1"), timeout=1)

    sent = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert sent[-2:] == hints
    assert len(sent) == (3 if after_deltas else 2)
    if after_deltas:
        assert sent[0]["payload"]["content"] == "firstsecond"
    assert queue not in channel._queued_moa_notices
    for index in range(5):
        channel._enqueue_send(ws, str(index))
    dequeue = Mock(wraps=queue.get_nowait)
    monkeypatch.setattr(queue, "get_nowait", dequeue)
    channel._enqueue_send(ws, "dropped")
    dequeue.assert_not_called()
    assert [queue.get_nowait() for _ in range(5)] == [str(index) for index in range(5)]


@pytest.mark.asyncio
async def test_disconnected_queue_counts_do_not_survive_reconnect():
    from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey

    channel = _channel()
    route = RoutingKey(
        user_id="u", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="s1",
    )
    ws = SimpleNamespace(closed=False, send=AsyncMock())
    await channel.register_ws(ws, route)
    old_queue = channel._send_queues[ws._jiuwen_ws_id]
    for status in ("started", "completed"):
        channel._enqueue_send(ws, {"type": "event", "event": "chat.moa_status", "payload": {"status": status}})
    assert channel._queued_moa_notices[old_queue] == 2
    ws.closed = True
    await channel.unregister_ws(ws)
    assert ws._jiuwen_ws_id not in channel._send_queues

    replacement = SimpleNamespace(closed=False, send=AsyncMock())
    await channel.register_ws(replacement, route)
    try:
        new_queue = channel._send_queues[replacement._jiuwen_ws_id]
        assert new_queue is not old_queue
        assert new_queue not in channel._queued_moa_notices
        old_queue_ref = ref(old_queue)
        del old_queue
        gc.collect()
        assert old_queue_ref() is None
        assert not channel._queued_moa_notices
    finally:
        await channel.unregister_ws(replacement)
