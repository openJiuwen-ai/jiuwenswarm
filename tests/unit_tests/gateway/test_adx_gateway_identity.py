# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from jiuwenswarm.extensions.adx.config import load_router_config
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient
from gateway.extensions.auth.credential_authenticator import AuthResult
from gateway.app_gateway import GatewayServer, GatewayServerConfig, RouteConfig
from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.tui.tui_channel import TuiChannel
from gateway.channel_manager.web.web_channel_app import build_web_channel_app
from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def _client(monkeypatch):
    monkeypatch.setenv("ADX_TEST_PLATFORM", "platform-key")
    return AdxRouterClient(load_router_config({"gateway": {"adx": {
        "ingress_url": "https://adx.example", "tenant": "appliance", "template": "jiuwen", "version": "v1",
        "token_env": "ADX_TEST_PLATFORM",
    }}}))


def test_web_keeps_business_identity_and_existing_authentication(monkeypatch):
    business_user = "new-jiuwen-user"
    client = _client(monkeypatch)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter(), agent_client=client)
    existing_auth = AsyncMock(return_value=AuthResult(True, user_id="platform-iam-uuid"))
    channel.set_handshake_auth(existing_auth)
    client.set_channel_manager(SimpleNamespace(get_channel=lambda kind: channel if kind.value == "web" else None, subscribe_channel_events=lambda callback: None))

    async def accepted(ws, path=None):
        identity = channel._resolve_connection_user_id({"user_id": business_user}, ws)
        await ws.send(identity)
        await ws.close()

    monkeypatch.setattr(channel, "handle_connection", accepted)
    with TestClient(build_web_channel_app(channel)) as transport:
        with transport.websocket_connect(f"/ws?user_id={business_user}") as ws:
            assert ws.receive_text() == business_user
    existing_auth.assert_awaited_once()


async def test_tui_keeps_business_user_header_and_existing_authentication(monkeypatch):
    client = _client(monkeypatch)
    channel = TuiChannel()
    existing_auth = AsyncMock(return_value=AuthResult(True, user_id="platform-iam-uuid"))
    channel.set_handshake_auth(existing_auth)
    client.set_channel_manager(SimpleNamespace(get_channel=lambda kind: channel if kind.value == "tui" else None, subscribe_channel_events=lambda callback: None))
    server = GatewayServer(
        GatewayServerConfig(enabled=True, routes={"/tui": RouteConfig(path="/tui", channel_id="tui", ws_channel=channel)}),
        RobotMessageRouter(),
    )

    class Socket:
        path = "/tui"
        request_headers = {"X-User-Id": "alice"}
        remote_address = ("127.0.0.1", 12345)
        send = AsyncMock()
        close = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = Socket()
    assert await server._process_request("/tui", socket.request_headers) is None
    await server._connection_handler(socket)
    assert socket._gateway_user_id == "alice"
    existing_auth.assert_awaited_once()


@pytest.mark.parametrize("backend", ["adx_router", "agentos_router"])
async def test_extension_registers_only_when_adx_backend_is_selected(monkeypatch, backend):
    from jiuwenswarm.extensions.adx import extension

    # A Yuanrong deployment must start without any ADX settings or credentials.
    settings = {"gateway": {"agent_client": {"type": backend}}}
    if backend == "adx_router":
        monkeypatch.setenv("ADX_TEST_PLATFORM", "platform-key")
        settings["gateway"]["adx"] = {
            "ingress_url": "https://adx.example", "tenant": "appliance", "template": "jiuwen",
            "version": "v1", "token_env": "ADX_TEST_PLATFORM",
        }
    monkeypatch.setattr(extension, "get_config", lambda: settings)
    registered = []
    registry = SimpleNamespace(register_agent_server_client=registered.append)
    extensions = await extension.register_extensions(registry)
    if backend == "agentos_router":
        assert extensions == registered == []
        return
    assert len(extensions) == 1 and extensions == registered
    assert extensions[0].metadata.id == "adx_router"
    assert isinstance(extensions[0].get_client(), AdxRouterClient)
    await extensions[0].shutdown()


@pytest.mark.parametrize("method", ["history.get", "chat.send", "command.goal"])
async def test_adx_cron_snapshot_is_turn_only_and_owner_scoped(monkeypatch, method):
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from gateway.message_handler.message_handler import MessageHandler

    client = _client(monkeypatch)
    jobs = [{"id": "alice-job", "user_id": "alice"}]
    controller = SimpleNamespace(list_jobs=AsyncMock(return_value=jobs))
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._cron_controller = controller
    send = AsyncMock(return_value=SimpleNamespace(ok=True))
    monkeypatch.setattr(
        "gateway.message_handler.message_handler.send_agent_request_with_timeout", send,
    )
    try:
        await handler._sync_agentos_cron_jobs(E2AEnvelope(
            request_id="req-1", method=method, user_id="alice", channel="web", session_id="session-1",
        ))
        if method == "history.get":
            controller.list_jobs.assert_not_awaited()
            send.assert_not_awaited()
        else:
            controller.list_jobs.assert_awaited_once_with(user_id="alice")
            send.assert_awaited_once()
            snapshot = send.await_args.args[1]
            assert snapshot.method == "cron.jobs.sync"
            assert snapshot.user_id == "alice" and snapshot.params["jobs"] == jobs
    finally:
        await client.shutdown()
