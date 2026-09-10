# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stale ws_id stream events must not session-fallback into another peer.

Covers chat.delta and chat.processing_status (both request-scoped).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.routing.keys import RoutingKey


class _FakeClient:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed = False
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))


def _delta_msg(*, session_id: str, ws_id: str, content: str) -> Message:
    return Message(
        id="delta-1",
        type="event",
        channel_id="web",
        session_id=session_id,
        params={},
        timestamp=0.0,
        ok=True,
        payload={"event_type": "chat.delta", "content": content, "session_id": session_id},
        event_type=EventType.CHAT_DELTA,
        metadata={"ws_id": ws_id},
    )


def _status_msg(*, session_id: str, ws_id: str) -> Message:
    return Message(
        id="status-1",
        type="event",
        channel_id="web",
        session_id=session_id,
        params={},
        timestamp=0.0,
        ok=True,
        payload={"event_type": "chat.processing_status", "is_processing": True, "session_id": session_id},
        event_type=EventType.CHAT_PROCESSING_STATUS,
        metadata={"ws_id": ws_id},
    )


async def _flush(client: _FakeClient) -> None:
    for _ in range(20):
        if client.frames:
            return
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_stale_ws_id_chat_delta_does_not_fallback_to_session_peer():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    live = _FakeClient()
    routing_key = RoutingKey(
        channel_id="web",
        app_id="default",
        user_id="u1",
        session_id="sess-1",
        agent_ref=None,
    )
    await channel.register_ws(live, routing_key)
    try:
        await channel.send(_delta_msg(session_id="sess-1", ws_id="dead-outbound", content="STALE"))
        await asyncio.sleep(0.03)
        assert live.frames == []
    finally:
        await channel.unregister_ws(live)


@pytest.mark.asyncio
async def test_live_ws_id_chat_delta_still_delivers():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    live = _FakeClient()
    routing_key = RoutingKey(
        channel_id="web",
        app_id="default",
        user_id="u1",
        session_id="sess-1",
        agent_ref=None,
    )
    await channel.register_ws(live, routing_key)
    try:
        ws_id = str(getattr(live, "_jiuwen_ws_id", "") or "")
        await channel.send(_delta_msg(session_id="sess-1", ws_id=ws_id, content="ok"))
        await _flush(live)
        assert live.frames
        assert live.frames[0]["event"] == "chat.delta"
        assert live.frames[0]["payload"]["content"] == "ok"
    finally:
        await channel.unregister_ws(live)


@pytest.mark.asyncio
async def test_stale_ws_id_processing_status_does_not_fallback_to_session_peer():
    # 与 chat.delta 一致：旧 HTTP SSE 的 processing_status 不得 session 兜底进新 outbound，
    # 否则企业版 SSE 会被异 rid 的 is_processing=false 误结束。
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    live = _FakeClient()
    routing_key = RoutingKey(
        channel_id="web",
        app_id="default",
        user_id="u1",
        session_id="sess-1",
        agent_ref=None,
    )
    await channel.register_ws(live, routing_key)
    try:
        await channel.send(_status_msg(session_id="sess-1", ws_id="dead-outbound"))
        await asyncio.sleep(0.03)
        assert live.frames == []
    finally:
        await channel.unregister_ws(live)


@pytest.mark.asyncio
async def test_live_ws_id_processing_status_still_delivers():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    live = _FakeClient()
    routing_key = RoutingKey(
        channel_id="web",
        app_id="default",
        user_id="u1",
        session_id="sess-1",
        agent_ref=None,
    )
    await channel.register_ws(live, routing_key)
    try:
        ws_id = str(getattr(live, "_jiuwen_ws_id", "") or "")
        await channel.send(_status_msg(session_id="sess-1", ws_id=ws_id))
        await _flush(live)
        assert live.frames
        assert live.frames[0]["event"] == "chat.processing_status"
    finally:
        await channel.unregister_ws(live)
