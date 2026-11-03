# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BaseWsChannel outbound queue: capped, drops instead of piling up memory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from gateway.routing import base_ws_channel


def _channel() -> WebChannel:
    return WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())


@pytest.mark.asyncio
async def test_register_ws_bounds_the_outbound_queue():
    from gateway.routing.keys import AgentRef, RoutingKey

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


def test_enqueue_send_drops_frames_once_the_queue_is_full():
    channel = _channel()
    ws = SimpleNamespace(closed=False, _jiuwen_ws_id="ws-1")
    channel._send_queues["ws-1"] = asyncio.Queue(maxsize=2)

    for i in range(5):
        channel._enqueue_send(ws, f"f{i}")

    queue = channel._send_queues["ws-1"]
    assert queue.qsize() == 2
    assert [queue.get_nowait(), queue.get_nowait()] == ["f0", "f1"]


def test_enqueue_send_ignores_closed_ws():
    channel = _channel()
    ws = SimpleNamespace(closed=True, _jiuwen_ws_id="ws-1")
    channel._send_queues["ws-1"] = asyncio.Queue()

    channel._enqueue_send(ws, "frame")

    assert channel._send_queues["ws-1"].qsize() == 0
