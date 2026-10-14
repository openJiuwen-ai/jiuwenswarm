"""JM-10: JiuwenMate reply fragments reach the requesting Web connection with their full payload."""

import asyncio
import json

import pytest
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.routing.keys import RoutingKey

STREAM = {
    "event_type": "jiuwenmate.chat.stream",
    "kind": "delta",
    "agent_id": "mate_dev",
    "request_id": "req_1",
    "execution_id": "exe_1",
    "seq": 3,
    "text": "**进展**",
}


class _FakeClient:
    def __init__(self):
        self.frames = []
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, data):
        self.frames.append(json.loads(data))


@pytest.mark.asyncio
async def test_stream_push_without_session_reaches_only_its_connection():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    mine, other = _FakeClient(), _FakeClient()
    for client in (mine, other):
        key = RoutingKey(channel_id="web", app_id="default", user_id="local", session_id="s-1", agent_ref=None)
        await channel.register_ws(client, key)
    try:
        msg = Message(
            id="exe_1:3",
            type="event",
            channel_id="web",
            session_id=None,
            params={},
            timestamp=1.0,
            ok=True,
            payload=dict(STREAM),
            metadata={"ws_id": getattr(mine, "_jiuwen_ws_id", "")},
        )

        await channel.send(msg)
        for _ in range(20):
            if mine.frames:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)

        assert mine.frames == [{"type": "event", "event": "jiuwenmate.chat.stream", "payload": STREAM}]
        assert other.frames == []
    finally:
        await channel.unregister_ws(mine)
        await channel.unregister_ws(other)
