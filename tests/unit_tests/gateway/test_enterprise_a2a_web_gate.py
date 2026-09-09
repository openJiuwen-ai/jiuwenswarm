# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import socket

import httpx
import pytest

from jiuwenswarm.gateway import app_gateway
from jiuwenswarm.gateway.a2a_manager import A2AIngressConfig, A2AIngressError, A2AManager
from jiuwenswarm.gateway.a2a_manager import manager as a2a_manager_module
from jiuwenswarm.gateway.channel_manager.web import (
    app_web_handlers,
    invoke as web_invoke,
    web_http_app,
)


class _WebChannelProbe:
    def __init__(self) -> None:
        self.methods = {}

    def register_method(self, name, handler) -> None:
        self.methods[name] = handler

    def on_connect(self, handler) -> None:
        self.on_connect_handler = handler


class _IngressManagerProbe:
    def __init__(self) -> None:
        self.started = False

    async def start_from_config(self) -> None:
        self.started = True


class _ConfigRepositoryProbe:
    def __init__(self) -> None:
        self.saved = []

    def save(self, config) -> None:
        self.saved.append(config)


class _OutboundSettingsRepositoryProbe:
    def load(self) -> dict[str, bool]:
        return {"allow_loopback_http": False}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_enterprise_a2a_web_gate_only_allows_directory_toggle_and_history(
    monkeypatch,
):
    monkeypatch.setattr(web_invoke, "is_enterprise", lambda: True)
    allowed = {
        "a2a.outbound.list",
        "a2a.outbound.enabled.update",
        "a2a.outbound.dispatch.list",
        "a2a.outbound.dispatch.get",
    }
    blocked = {
        "a2a.outbound.settings.get",
        "a2a.outbound.settings.update",
        "a2a.outbound.discover",
        "a2a.outbound.register",
        "a2a.outbound.get",
        "a2a.outbound.edit",
        "a2a.outbound.update",
        "a2a.outbound.refresh",
        "a2a.outbound.confirm_revision",
        "a2a.outbound.delete",
        "a2a.ingress.get",
        "a2a.ingress.edit",
        "a2a.ingress.history",
        "a2a.ingress.update",
        "a2a.ingress.enable",
        "a2a.ingress.disable",
        "a2a.ingress.reload",
    }

    assert all(not web_invoke.is_enterprise_write_forbidden(item) for item in allowed)
    assert {
        "a2a.outbound.list",
        "a2a.outbound.enabled.update",
        "a2a.outbound.dispatch.get",
    } <= web_invoke._LOCAL_ROUTING_IDENTITY_METHODS
    assert all(web_invoke.is_enterprise_write_forbidden(item) for item in blocked)
    monkeypatch.setattr(web_invoke, "is_enterprise", lambda: False)
    assert all(not web_invoke.is_enterprise_write_forbidden(item) for item in blocked)


def test_enterprise_does_not_register_a2a_ingress_rpc_handlers(monkeypatch):
    monkeypatch.setattr(app_web_handlers, "is_enterprise", lambda: True)
    channel = _WebChannelProbe()

    app_web_handlers._register_web_handlers(
        app_web_handlers.WebHandlersBindParams(channel=channel)
    )

    assert not any(name.startswith("a2a.ingress.") for name in channel.methods)
    assert "a2a.outbound.list" in channel.methods


@pytest.mark.asyncio
async def test_enterprise_does_not_mount_or_advertise_a2a_ingress_http_routes(
    monkeypatch,
):
    monkeypatch.setattr(web_http_app, "is_enterprise", lambda: True)
    app = web_http_app.create_web_http_app(object())
    paths = {route.path for route in app.routes}

    assert not any(path.startswith("/api/v1/a2a/ingress") for path in paths)
    assert "/api/v1/a2a/outbound/agents" in paths

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog")
        missing = [
            await client.get("/api/v1/a2a/ingress"),
            await client.get("/api/v1/a2a/ingress/history"),
            await client.get("/.well-known/agent-card.json"),
            await client.post("/a2a", json={}),
        ]

    assert response.status_code == 200
    assert all(item.status_code == 404 for item in missing)
    assert not any(
        str(row.get("rpc_method") or "").startswith("a2a.ingress.")
        for row in response.json()["data"]["routes"]
    )


@pytest.mark.asyncio
async def test_enterprise_ignores_ingress_environment_and_never_starts_listener(
    monkeypatch,
):
    monkeypatch.setattr(app_gateway, "is_enterprise", lambda: True)
    monkeypatch.setenv("A2A_SERVER_ENABLED", "true")

    config, error = app_gateway._load_gateway_a2a_ingress_config()
    manager = _IngressManagerProbe()
    await app_gateway._start_gateway_a2a_ingress(manager)

    assert config.enabled is False
    assert error is None
    assert manager.started is False


@pytest.mark.asyncio
async def test_personal_edition_keeps_a2a_ingress_startup(monkeypatch):
    monkeypatch.setattr(app_gateway, "is_enterprise", lambda: False)
    manager = _IngressManagerProbe()

    await app_gateway._start_gateway_a2a_ingress(manager)

    assert manager.started is True


def test_personal_edition_keeps_a2a_ingress_rpc_and_http_routes(monkeypatch):
    monkeypatch.setattr(app_web_handlers, "is_enterprise", lambda: False)
    monkeypatch.setattr(web_http_app, "is_enterprise", lambda: False)
    channel = _WebChannelProbe()
    app_web_handlers._register_web_handlers(
        app_web_handlers.WebHandlersBindParams(channel=channel)
    )
    paths = {route.path for route in web_http_app.create_web_http_app(object()).routes}

    assert "a2a.ingress.get" in channel.methods
    assert "a2a.ingress.update" in channel.methods
    assert "/api/v1/a2a/ingress" in paths
    assert "/api/v1/a2a/ingress/history" in paths


@pytest.mark.asyncio
async def test_enterprise_manager_hard_gate_never_binds_ingress_port(monkeypatch):
    monkeypatch.setattr(a2a_manager_module, "is_enterprise", lambda: True)
    port = _free_port()
    manager = A2AManager(
        object(),
        object(),
        A2AIngressConfig(enabled=True, host="127.0.0.1", port=port),
        repository=_ConfigRepositoryProbe(),
        outbound_settings_repository=_OutboundSettingsRepositoryProbe(),
    )

    with pytest.raises(A2AIngressError) as exc_info:
        await manager.start_from_config()

    assert exc_info.value.code == "A2A_INGRESS_DISABLED"
    server = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", port)
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "update", "reload"])
async def test_enterprise_manager_rejects_all_ingress_start_operations(
    monkeypatch,
    operation,
):
    monkeypatch.setattr(a2a_manager_module, "is_enterprise", lambda: True)
    repository = _ConfigRepositoryProbe()
    manager = A2AManager(
        object(),
        object(),
        A2AIngressConfig(),
        repository=repository,
        outbound_settings_repository=_OutboundSettingsRepositoryProbe(),
    )

    with pytest.raises(A2AIngressError) as exc_info:
        if operation == "update":
            await manager.update({"enabled": True}, apply=True)
        else:
            await getattr(manager, operation)()

    assert exc_info.value.code == "A2A_INGRESS_DISABLED"
    assert repository.saved == []
