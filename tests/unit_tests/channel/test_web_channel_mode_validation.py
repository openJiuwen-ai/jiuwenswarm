# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reject malformed request modes before WebSocket routing (issue #7299)."""

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.message import Mode, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey


class _WebSocket:
    remote_address = ("127.0.0.1", 12345)

    def __init__(self):
        self.frames = []
        self.received = asyncio.Event()

    async def send(self, raw):
        self.frames.append(json.loads(raw))
        self.received.set()


def _interrupt(mode):
    return json.dumps(
        {
            "type": "req",
            "id": "interrupt-mode",
            "method": "chat.interrupt",
            "params": {
                "session_id": "sess-test",
                "intent": "pause",
                "mode": mode,
                "team": True,
            },
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [["team"], {}, [], 1, True, None])
async def test_interrupt_invalid_mode_returns_bad_request_without_dispatch(mode):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _WebSocket()
    original_key = RoutingKey(
        "local", "web", "default", AgentRef.default(), "sess-original"
    )
    seen = []
    channel.on_message(lambda message: seen.append(message))
    await channel.register_ws(ws, original_key)
    try:
        await channel._handle_raw_message(ws, _interrupt(mode), {})
        await asyncio.wait_for(ws.received.wait(), timeout=1)

        assert ws.frames == [
            {
                "type": "res",
                "id": "interrupt-mode",
                "ok": False,
                "payload": {},
                "error": "mode must be a string",
                "code": "BAD_REQUEST",
            }
        ]
        assert seen == []
        assert list(channel._clients_by_key) == [original_key]
        assert id(ws) not in channel._ws_sessions
    finally:
        await channel.unregister_ws(ws)


@pytest.mark.asyncio
async def test_interrupt_valid_team_mode_preserves_dispatch_and_response():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _WebSocket()
    seen = []
    channel.on_message(lambda message: seen.append(message))

    async def interrupt_handler(socket, req_id, params, session_id):
        await channel.send_response(
            socket,
            req_id,
            ok=True,
            payload={
                "accepted": True,
                "session_id": session_id,
                "intent": params["intent"],
            },
        )

    channel.register_method("chat.interrupt", interrupt_handler)
    try:
        await channel._handle_raw_message(ws, _interrupt("team"), {})
        await asyncio.wait_for(ws.received.wait(), timeout=1)

        assert ws.frames == [
            {
                "type": "res",
                "id": "interrupt-mode",
                "ok": True,
                "payload": {
                    "accepted": True,
                    "session_id": "sess-test",
                    "intent": "pause",
                },
            }
        ]
        assert len(seen) == 1
        assert seen[0].req_method == ReqMethod.CHAT_CANCEL
        assert seen[0].params["mode"] == "team"
        assert seen[0].mode == Mode.from_raw("team")
        assert seen[0].agent_ref == {"mode": "team", "id": "default"}
        assert channel._ws_sessions[id(ws)] == {"sess-test"}
    finally:
        await channel.unregister_ws(ws)
