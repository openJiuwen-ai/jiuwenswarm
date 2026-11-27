from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock

import pytest

from jiuwenclaw.agentserver.gateway_push.transport import WebSocketGatewayPushTransport


@pytest.mark.asyncio
async def test_websocket_gateway_push_transport_forwards_to_ws_server(monkeypatch) -> None:
    send_push = AsyncMock()
    fake_server_instance = types.SimpleNamespace(send_push=send_push)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return fake_server_instance

    fake_module = types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer)
    monkeypatch.setitem(sys.modules, "jiuwenclaw.agentserver.agent_ws_server", fake_module)

    transport = WebSocketGatewayPushTransport()
    msg = {
        "request_id": "req-1",
        "channel_id": "web",
        "payload": {"event_type": "chat.delta", "content": "hello"},
    }
    await transport.send_push(msg)

    send_push.assert_awaited_once_with(msg)
