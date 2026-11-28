from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport


@pytest.mark.asyncio
async def test_websocket_gateway_push_transport_forwards_to_active_runtime_host() -> None:
    from jiuwenswarm.runtime.host_services import (
        install_runtime_push_handler, restore_runtime_push_handler,
    )

    send_push = AsyncMock(return_value=True)
    previous = install_runtime_push_handler(send_push)

    transport = WebSocketGatewayPushTransport()
    msg = {
        "request_id": "req-1",
        "channel_id": "web",
        "payload": {"event_type": "chat.delta", "content": "hello"},
    }
    try:
        assert await transport.send_push(msg) is True
    finally:
        restore_runtime_push_handler(send_push, previous)
    send_push.assert_awaited_once_with(msg)
