"""Owner sharing actions cross the normal Web Gateway instead of a local fallback."""

import json

import pytest

from agent_gateway.app_gateway import RouteConfig
from tests.unit_tests.gateway.test_app_gateway_acp import build_server, FakeWebSocket


@pytest.mark.parametrize("method", ["jiuwenmate.sharing.list", "jiuwenmate.sharing.decide", "jiuwenmate.sharing.revoke"])
async def test_sharing_methods_are_registered_and_forwarded_to_agent_server(method):
    server = build_server()
    server.config.routes["/web"] = RouteConfig(
        path="/web", channel_id="web",
    )
    seen = []

    async def receive(message):
        seen.append(message)

    server.on_message(receive)
    ws = FakeWebSocket()
    await server.handle_raw_message_public(ws, json.dumps({
        "type": "req", "id": "sharing-rpc", "method": method,
        "params": {"agent_id": "config-test", "session_id": "session-test"},
    }), path="/web")
    assert len(seen) == 1
    assert seen[0].req_method == method
    assert seen[0].params["agent_id"] == "config-test"
    assert not ws.sent_frames

