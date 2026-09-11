# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent Web UI proxy mounted on WebChannel (:19000)."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp
import httpx
import pytest
from aiohttp import web

from jiuwenswarm.gateway.channel_manager.base import ChannelType, RobotMessageRouter
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    RESERVED_AGENT_TYPES,
    WEB_PROXY_COOKIE_NAME,
    WebProxyChannelConfig,
    _append_tail,
)
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_channel_type_includes_web_proxy() -> None:
    assert ChannelType.WEB_PROXY.value == "web_proxy"


def test_web_proxy_config_from_dict_defaults() -> None:
    cfg = WebProxyChannelConfig.from_dict(None)
    assert cfg.enabled is False
    cfg = WebProxyChannelConfig.from_dict({"enabled": True, "listen_port": 19111})
    assert cfg.enabled is True


def test_append_tail_yuanrong_query_url() -> None:
    url = "http://yr:8888/serverless/v1/http?instance=x&port=18789"
    out = _append_tail(url, "assets/app.js")
    assert out.startswith(url)
    assert "path=%2Fassets%2Fapp.js" in out


def test_append_tail_plain_url_joins_path() -> None:
    assert _append_tail("http://127.0.0.1:8080", "assets/app.js") == (
        "http://127.0.0.1:8080/assets/app.js"
    )
    assert _append_tail("http://127.0.0.1:8080/", "assets/app.js") == (
        "http://127.0.0.1:8080/assets/app.js"
    )


def test_append_tail_empty_is_noop() -> None:
    url = "http://127.0.0.1:8080/openclaw"
    assert _append_tail(url, "") is url


def test_reserved_agent_types_cover_webchannel_paths() -> None:
    assert "ws" in RESERVED_AGENT_TYPES
    assert "file-api" in RESERVED_AGENT_TYPES


class _FakeIamClient:
    def __init__(
        self,
        *,
        user_id: str = "iam-user",
        token: str = "good",
        auth_enabled: bool = True,
        username: str = "",
    ) -> None:
        self.auth_enabled = auth_enabled
        self.user_id = user_id
        self.token = token
        self.username = username
        self.calls: list[dict[str, Any]] = []

    async def authenticate_http(
        self,
        *,
        path: str,
        headers: Any,
        remote: str = "",
        channel: str = "file-api",
        allow_query_token: bool = True,
    ) -> AuthResult:
        from jiuwenswarm.extensions.agentos.auth.common import (
            extract_token_from_path_and_headers,
        )

        token_path = path if allow_query_token else ""
        token = extract_token_from_path_and_headers(token_path, headers)
        self.calls.append({"path": path, "token": token, "channel": channel, "remote": remote})
        if not self.auth_enabled:
            return AuthResult(success=True, user_id="")
        if not token:
            return AuthResult(
                success=False,
                error="缺少 token",
                extensions={"error_code": "MISSING_TOKEN"},
            )
        if token != self.token:
            return AuthResult(
                success=False,
                error="invalid token",
                extensions={"error_code": "UNAUTHORIZED"},
            )
        extensions: dict[str, Any] = {}
        if self.username:
            extensions["username"] = self.username
        return AuthResult(success=True, user_id=self.user_id, extensions=extensions)


def _proxy_channel(
    *,
    resolver,
    enabled: bool = True,
    iam: _FakeIamClient | None = None,
) -> WebChannel:
    channel = WebChannel(
        WebChannelConfig(enabled=True, dual_protocol=True, host="127.0.0.1"),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_proxy_enabled = enabled
    if iam is not None:
        channel.agent_client = iam
    return channel


async def _asgi_client(channel: WebChannel) -> httpx.AsyncClient:
    app = build_web_channel_app(channel)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)


@pytest.mark.asyncio
async def test_http_bare_path_redirects_to_slash() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run for 301")

    channel = _proxy_channel(resolver=resolver)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw?user_id=u1")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/openclaw/?user_id=u1"


@pytest.mark.asyncio
async def test_http_missing_user_id_returns_400() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run")

    channel = _proxy_channel(resolver=resolver)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw/")
    assert resp.status_code == 400
    assert "user_id" in resp.text


@pytest.mark.asyncio
async def test_http_disabled_returns_404() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run")

    channel = _proxy_channel(resolver=resolver, enabled=False)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw/?user_id=u1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_http_reserved_ws_path_not_proxied() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not steal /ws")

    channel = _proxy_channel(resolver=resolver)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/ws?user_id=u1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_http_referer_fallback_and_tail_forward() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["path"] = request.path
        return web.Response(text="asset-ok", content_type="application/javascript")

    upstream_app = web.Application()
    upstream_app.router.add_get("/{tail:.*}", upstream_handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    upstream_port = int(site._server.sockets[0].getsockname()[1])

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        captured["user_id"] = user_id
        captured["agent_type"] = agent_type
        captured["protocol"] = protocol
        return f"http://127.0.0.1:{upstream_port}"

    channel = _proxy_channel(resolver=resolver)
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get(
                "/openclaw/assets/app.js",
                headers={"Referer": "http://test/openclaw/?user_id=u1"},
            )
        assert resp.status_code == 200
        assert resp.text == "asset-ok"
        assert resp.headers["content-type"].startswith("application/javascript")
        assert captured["user_id"] == "u1"
        assert captured["agent_type"] == "openclaw"
        assert captured["protocol"] == "http"
        assert captured["path"] == "/assets/app.js"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_resolver_miss_returns_404() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return None

    channel = _proxy_channel(resolver=resolver)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw/?user_id=u1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_build_app_keeps_ws_and_adds_agent_catch_all() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None)
    app = build_web_channel_app(channel)
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/ws" in paths
    assert "/ws/git" in paths
    assert "/{agent_type}" in paths
    assert "/{agent_type}/{tail:path}" in paths
    assert "/file-api/upload" not in paths


@pytest.mark.asyncio
async def test_ws_proxy_pumps_text() -> None:
    async def upstream_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{msg.data}")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                break
        return ws

    upstream_app = web.Application()
    upstream_app.router.add_get("/ws", upstream_ws)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    upstream_port = int(site._server.sockets[0].getsockname()[1])

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        assert protocol == "ws"
        assert user_id == "u1"
        assert agent_type == "openclaw"
        return f"http://127.0.0.1:{upstream_port}"

    proxy_port = _free_port()
    channel = WebChannel(
        WebChannelConfig(
            enabled=True,
            dual_protocol=True,
            host="127.0.0.1",
            port=proxy_port,
        ),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_proxy_enabled = True
    task = asyncio.create_task(channel.start(), name="web-proxy-ws-test")
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            srv = channel._uvicorn_server
            if srv is not None and getattr(srv, "started", False):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("WebChannel did not start")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"http://127.0.0.1:{proxy_port}/openclaw/ws?user_id=u1"
            ) as ws:
                await ws.send_str("ping")
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.TEXT
                assert msg.data == "echo:ping"
    finally:
        await channel.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await runner.cleanup()


def _cookie_header(response: httpx.Response) -> str:
    raw = response.headers.get("set-cookie") or ""
    assert WEB_PROXY_COOKIE_NAME in raw
    return raw.split(";", 1)[0]


@pytest.mark.asyncio
async def test_http_auth_missing_token_returns_401() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run without a token")

    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=resolver, iam=iam)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw/?user_id=iam-user")
    assert resp.status_code == 401
    assert resp.json()["code"] == "MISSING_TOKEN"
    assert iam.calls
    assert iam.calls[0]["channel"] == "web-proxy"


@pytest.mark.asyncio
async def test_http_auth_query_token_sets_cookie_and_strips_redirect() -> None:
    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=lambda *_a, **_k: None, iam=iam)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw?user_id=iam-user&token=good")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/openclaw/?user_id=iam-user"
    assert "token=" not in resp.headers["location"]
    assert WEB_PROXY_COOKIE_NAME in (resp.headers.get("set-cookie") or "")


@pytest.mark.asyncio
async def test_http_auth_bearer_and_cookie_followup_uses_iam_user() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["cookie"] = request.headers.get("Cookie", "")
        return web.Response(text="ui-ok")

    upstream_app = web.Application()
    upstream_app.router.add_get("/{tail:.*}", upstream_handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    upstream_port = int(site._server.sockets[0].getsockname()[1])

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        captured["user_id"] = user_id
        captured["agent_type"] = agent_type
        return f"http://127.0.0.1:{upstream_port}"

    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=resolver, iam=iam)
    try:
        async with await _asgi_client(channel) as client:
            first = await client.get(
                "/openclaw/",
                headers={"Authorization": "Bearer good"},
            )
            assert first.status_code == 200
            assert first.text == "ui-ok"
            assert captured["user_id"] == "iam-user"
            assert captured["auth"] == ""
            assert captured["cookie"] == ""
            cookie = _cookie_header(first)

            captured.clear()
            second = await client.get(
                "/openclaw/assets/app.js",
                headers={"Cookie": cookie},
            )
        assert second.status_code == 200
        assert captured["user_id"] == "iam-user"
        assert iam.calls[-1]["token"] == "good"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_auth_user_id_mismatch_returns_403() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run on mismatch")

    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=resolver, iam=iam)
    async with await _asgi_client(channel) as client:
        resp = await client.get("/openclaw/?user_id=other&token=good")
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_MISMATCH"


@pytest.mark.asyncio
async def test_http_auth_disabled_still_uses_query_user_id() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        del request
        return web.Response(text="ok")

    upstream_app = web.Application()
    upstream_app.router.add_get("/{tail:.*}", upstream_handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    upstream_port = int(site._server.sockets[0].getsockname()[1])

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        captured["user_id"] = user_id
        return f"http://127.0.0.1:{upstream_port}"

    iam = _FakeIamClient(auth_enabled=False)
    channel = _proxy_channel(resolver=resolver, iam=iam)
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get("/openclaw/?user_id=u1")
        assert resp.status_code == 200
        assert captured["user_id"] == "u1"
        assert WEB_PROXY_COOKIE_NAME not in (resp.headers.get("set-cookie") or "")
        assert not iam.calls
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ws_auth_missing_token_rejects_handshake() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run without a token")

    iam = _FakeIamClient()
    proxy_port = _free_port()
    channel = WebChannel(
        WebChannelConfig(
            enabled=True,
            dual_protocol=True,
            host="127.0.0.1",
            port=proxy_port,
        ),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_proxy_enabled = True
    channel.agent_client = iam
    task = asyncio.create_task(channel.start(), name="web-proxy-ws-auth-test")
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            srv = channel._uvicorn_server
            if srv is not None and getattr(srv, "started", False):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("WebChannel did not start")

        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await session.ws_connect(
                    f"http://127.0.0.1:{proxy_port}/openclaw/ws?user_id=iam-user"
                )
    finally:
        await channel.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
