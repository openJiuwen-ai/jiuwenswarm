import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["describe", "prepare", "commit", "status", "records"]
)
async def test_publish_wire_forwards_user_context_without_echoing_auth(method):
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_REQ_METHODS,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
    )

    value = "assets.publish." + method
    assert value in _FORWARD_REQ_METHODS and value in _FORWARD_NO_LOCAL_HANDLER_METHODS
    server = object.__new__(AgentWebSocketServer)
    api = SimpleNamespace(
        call=AsyncMock(
            return_value={"operation_id": "op", "execution_status": "queued"}
        )
    )
    server._get_asset_publish_api = AsyncMock(return_value=api)
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(
        request_id="request",
        channel_id="web",
        user_id="gateway-verified",
        req_method=ReqMethod(value),
        params={"auth": {"access_token": "synthetic-secret"}},
    )
    await server._handle_asset_publish(ws, request, asyncio.Lock())
    assert api.call.await_args.kwargs["gateway_user"] == "gateway-verified"
    assert "synthetic-secret" not in str(ws.send.call_args)
    assert "queued" in str(ws.send.call_args)


@pytest.mark.asyncio
async def test_publish_failure_never_echoes_internal_exception_or_request():
    server = object.__new__(AgentWebSocketServer)
    server._get_asset_publish_api = AsyncMock(
        side_effect=RuntimeError("Bearer synthetic-secret /private/path")
    )
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(
        request_id="r", channel_id="web", req_method=ReqMethod.ASSETS_PUBLISH_DESCRIBE
    )
    await server._handle_asset_publish(ws, request, asyncio.Lock())
    wire = str(ws.send.call_args)
    assert (
        "PUBLISH_UNAVAILABLE" in wire
        and "synthetic-secret" not in wire
        and "/private/path" not in wire
    )
