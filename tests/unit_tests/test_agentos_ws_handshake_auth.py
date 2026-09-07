# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.app_gateway import GatewayServer, GatewayServerConfig, RouteConfig
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


async def _deny(**_kwargs: Any) -> AuthResult:
    return AuthResult(success=False, error="缺少 token")


@pytest.mark.asyncio
async def test_webchannel_process_request_rejects_unauthorized() -> None:
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.set_handshake_auth(_deny)
    status, _headers, body = await channel._process_request("/ws", {})
    assert int(status) == 401
    assert body == b"Unauthorized\n"


@pytest.mark.asyncio
async def test_gateway_server_process_request_rejects_tui_not_acp() -> None:
    tui = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    tui.set_handshake_auth(_deny)
    server = GatewayServer(
        GatewayServerConfig(
            enabled=True,
            routes={
                "/tui": RouteConfig(path="/tui", channel_id="tui", ws_channel=tui),
                "/acp": RouteConfig(path="/acp", channel_id="acp"),
            },
        ),
        RobotMessageRouter(),
    )
    status, _headers, body = await server._process_request("/tui", {})
    assert int(status) == 401
    assert await server._process_request("/acp", {}) is None
