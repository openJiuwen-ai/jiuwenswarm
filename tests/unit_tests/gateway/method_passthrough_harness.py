"""Behavior assertion for business methods crossing the real Web dispatcher."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


async def assert_web_method_forwarded(method: str) -> None:
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    upstream = AsyncMock(return_value=True)
    channel.on_message(upstream)
    peer = SimpleNamespace(remote_address=("127.0.0.1", 1), closed=False)
    params = {"opaque": {"values": [1, False, None]}}
    await channel._handle_raw_message(peer, json.dumps({
        "type": "req", "id": "contract", "method": method, "params": params,
    }), {})
    upstream.assert_awaited_once()
    message = upstream.await_args.args[0]
    assert type(message.req_method) is str
    assert message.req_method == method
    assert message.params == params
