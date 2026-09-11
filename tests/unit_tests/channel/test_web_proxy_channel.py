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

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    RESERVED_AGENT_TYPES,
    WEB_PROXY_COOKIE_NAME,
    WEB_PROXY_USER_COOKIE,
    WebProxyChannelConfig,
    _append_tail,
    _apply_referer_fallback,
    _forward_headers,
    _forward_ws_headers,
    _retry_after_seconds,
    _rewrite_agent_html,
    _sandbox_creating_response,
    _ensure_proxy_session,
)
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_web_proxy_config_from_dict_defaults() -> None:
    cfg = WebProxyChannelConfig.from_dict(None)
    assert cfg.enabled is False
    assert cfg.auth_enabled is True
    cfg = WebProxyChannelConfig.from_dict({"enabled": True, "listen_port": 19111})
    assert cfg.enabled is True
    assert cfg.auth_enabled is True
    cfg = WebProxyChannelConfig.from_dict({"enabled": True, "auth_enabled": False})
    assert cfg.auth_enabled is False


def test_append_tail_yuanrong_query_url() -> None:
    url = "http://yr:8888/serverless/v1/http?instance=x&port=18789&tenant_id=default"
    assert _append_tail(url, "api/foo") == (
        "http://yr:8888/serverless/v1/http/api/foo"
        "?instance=x&port=18789&tenant_id=default"
    )
    assert _append_tail(url, "assets/app.js") == (
        "http://yr:8888/serverless/v1/http/assets/app.js"
        "?instance=x&port=18789&tenant_id=default"
    )
    ws = "ws://yr:8888/serverless/v1/ws?instance=x&port=18789&tenant_id=default"
    assert _append_tail(ws, "socket") == (
        "ws://yr:8888/serverless/v1/ws/socket"
        "?instance=x&port=18789&tenant_id=default"
    )


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


def test_referer_fallback_rewrites_spa_assets() -> None:
    agent, user, tail = _apply_referer_fallback(
        agent_type="assets",
        user_id="",
        tail="index.js",
        referer="http://gw:19000/openclaw/?user_id=test1",
    )
    assert (agent, user, tail) == ("openclaw", "test1", "assets/index.js")
    agent, user, tail = _apply_referer_fallback(
        agent_type="openclaw",
        user_id="",
        tail="assets/index.js",
        referer="http://gw:19000/openclaw/?user_id=test1",
    )
    assert (agent, user, tail) == ("openclaw", "test1", "assets/index.js")


def test_rewrite_agent_html_prefixes_openclaw_assets() -> None:
    html = (
        b'<!doctype html><html data-openclaw-control-ui-base-path="">'
        b'<script src="/assets/index.js"></script>'
        b'<link rel="icon" href="/favicon.svg" />'
        b"</html>"
    )
    out = _rewrite_agent_html(html, "openclaw").decode()
    assert 'data-openclaw-control-ui-base-path="/openclaw"' in out
    assert 'src="/openclaw/assets/index.js"' in out
    assert 'href="/openclaw/favicon.svg"' in out
    again = _rewrite_agent_html(out.encode(), "openclaw").decode()
    assert again.count("/openclaw/openclaw/") == 0


def test_forward_headers_sets_non_loopback_xff_and_strips_iam(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    out = _forward_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "access_token=x",
            "X-User-Id": "test1",
            "X-Forwarded-User": "spoof",
            "X-Forwarded-For": "127.0.0.1",
            "Accept": "text/html",
        },
        client_host="172.31.12.19",
        user_id="test1",
    )
    assert out["X-Forwarded-For"] == "172.31.12.19"
    assert out["X-Forwarded-User"] == "test1"
    assert "Authorization" not in out
    assert "Cookie" not in out
    assert "X-User-Id" not in out
    assert out["Accept"] == "text/html"
    loopback = _forward_headers({"Accept": "*/*"}, client_host="127.0.0.1")
    assert "X-Forwarded-For" not in loopback


def test_forward_headers_prefixes_same_host_gateway_ip(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_HOST", "172.31.12.19")
    out = _forward_headers({"Accept": "text/html"}, client_host="172.31.12.19")
    assert out["X-Forwarded-For"] == "192.0.2.1, 172.31.12.19"


def test_forward_ws_headers_keeps_origin_and_strips_ws_keys(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_HOST", "172.31.12.19")
    monkeypatch.setenv("WEB_PORT", "19000")
    out = _forward_ws_headers(
        {
            "Origin": "http://1.95.65.197:19000",
            "Sec-WebSocket-Key": "abc",
            "Sec-WebSocket-Version": "13",
            "Cookie": "user_id=test1",
        },
        client_host="8.8.8.8",
        user_id="test1",
    )
    assert out["Origin"] == "http://1.95.65.197:19000"
    assert out["X-Forwarded-For"] == "8.8.8.8"
    assert out["X-Forwarded-User"] == "test1"
    assert "Sec-WebSocket-Key" not in out
    assert "Cookie" not in out
    synthesized = _forward_ws_headers({"Accept": "*/*"}, client_host="8.8.8.8")
    assert synthesized["Origin"] == "http://172.31.12.19:19000"


def test_sandbox_creating_response_sets_retry_after() -> None:
    class _Busy(RuntimeError):
        retry_after_seconds = 5

    exc = _Busy("creating")
    assert _retry_after_seconds(exc) == 5
    resp = _sandbox_creating_response(exc)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"


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
    releaser=None,
) -> WebChannel:
    channel = WebChannel(
        WebChannelConfig(enabled=True, host="127.0.0.1"),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_runtime_release = releaser
    channel.web_proxy_enabled = enabled
    channel.web_proxy_auth_enabled = True
    if iam is not None:
        channel.agent_client = iam
    return channel


async def _asgi_client(channel: WebChannel) -> httpx.AsyncClient:
    app = build_web_channel_app(channel)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)


@pytest.mark.asyncio
async def test_ensure_proxy_session_single_flight_under_concurrency() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None)
    sessions = await asyncio.gather(
        *[_ensure_proxy_session(channel) for _ in range(16)]
    )
    try:
        assert len({id(item) for item in sessions}) == 1
        assert channel.web_proxy_session is sessions[0]
        assert not sessions[0].closed
    finally:
        await sessions[0].close()
    again = await _ensure_proxy_session(channel)
    try:
        assert again is not sessions[0]
        assert channel.web_proxy_session is again
    finally:
        await again.close()


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
async def test_http_proxy_releases_web_runtime_after_response() -> None:
    releases: list[tuple[str, str]] = []

    async def upstream_handler(_request: web.Request) -> web.Response:
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
        del user_id, agent_type, protocol
        return f"http://127.0.0.1:{upstream_port}"

    async def releaser(user_id: str, agent_type: str) -> None:
        releases.append((user_id, agent_type))

    channel = _proxy_channel(resolver=resolver, releaser=releaser)
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get("/openclaw/?user_id=u1")
        assert resp.status_code == 200
        assert releases == [("u1", "openclaw")]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_referer_rewrites_root_absolute_assets() -> None:
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
        del protocol
        return f"http://127.0.0.1:{upstream_port}"

    channel = _proxy_channel(resolver=resolver)
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get(
                "/assets/index-BmPLvh3B.js",
                headers={"Referer": "http://gw:19000/openclaw/?user_id=u1"},
            )
        assert resp.status_code == 200
        assert resp.text == "asset-ok"
        assert captured["user_id"] == "u1"
        assert captured["agent_type"] == "openclaw"
        assert captured["path"] == "/assets/index-BmPLvh3B.js"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_html_rewrites_root_assets_to_agent_prefix() -> None:
    html = (
        '<html data-openclaw-control-ui-base-path="">'
        '<script src="/assets/app.js"></script></html>'
    )

    async def upstream_handler(request: web.Request) -> web.Response:
        del request
        return web.Response(text=html, content_type="text/html")

    upstream_app = web.Application()
    upstream_app.router.add_get("/{tail:.*}", upstream_handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    upstream_port = int(site._server.sockets[0].getsockname()[1])

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return f"http://127.0.0.1:{upstream_port}"

    channel = _proxy_channel(resolver=resolver)
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get("/openclaw/?user_id=u1")
        assert resp.status_code == 200
        assert 'data-openclaw-control-ui-base-path="/openclaw"' in resp.text
        assert 'src="/openclaw/assets/app.js"' in resp.text
        assert WEB_PROXY_USER_COOKIE in (resp.headers.get("set-cookie") or "")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_user_cookie_forwards_assets_without_query() -> None:
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
        del protocol
        return f"http://127.0.0.1:{upstream_port}"

    channel = _proxy_channel(resolver=resolver)
    channel.web_proxy_auth_enabled = False
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get(
                "/openclaw/assets/app.js",
                headers={"Cookie": f"{WEB_PROXY_USER_COOKIE}=u1"},
            )
        assert resp.status_code == 200
        assert captured["user_id"] == "u1"
        assert captured["agent_type"] == "openclaw"
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

    releases: list[tuple[str, str]] = []

    async def releaser(user_id: str, agent_type: str) -> None:
        releases.append((user_id, agent_type))

    proxy_port = _free_port()
    channel = WebChannel(
        WebChannelConfig(
            enabled=True,
            host="127.0.0.1",
            port=proxy_port,
        ),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_runtime_release = releaser
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
                f"http://127.0.0.1:{proxy_port}/openclaw/ws?user_id=u1",
                headers={"Origin": "http://127.0.0.1:19000"},
            ) as ws:
                await ws.send_str("ping")
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.TEXT
                assert msg.data == "echo:ping"
                assert releases == []
        deadline = asyncio.get_running_loop().time() + 1.0
        while not releases and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert releases == [("u1", "openclaw")]
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
        resp = await client.get("/openclaw?user_id=iam-user&access_token=good")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/openclaw/?user_id=iam-user"
    assert "access_token=" not in resp.headers["location"]
    assert "token=" not in resp.headers["location"]
    assert WEB_PROXY_COOKIE_NAME in (resp.headers.get("set-cookie") or "")
    assert "access_token=" in (resp.headers.get("set-cookie") or "")


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
async def test_http_auth_routes_claimed_username_not_iam_uuid() -> None:
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
        del agent_type, protocol
        return f"http://127.0.0.1:{upstream_port}"

    iam = _FakeIamClient(user_id="f2e69af3-caad-45e0-a014-81082ebf7161", username="test1")
    channel = _proxy_channel(resolver=resolver, iam=iam)
    try:
        async with await _asgi_client(channel) as client:
            claimed = await client.get("/openclaw/?user_id=test1&token=good")
            omitted = await client.get(
                "/openclaw/",
                headers={"Authorization": "Bearer good"},
            )
        assert claimed.status_code == 200
        assert omitted.status_code == 200
        assert captured["user_id"] == "test1"
    finally:
        await runner.cleanup()


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
async def test_http_web_proxy_auth_flag_skips_iam() -> None:
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
        del agent_type, protocol
        return f"http://127.0.0.1:{upstream_port}"

    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=resolver, iam=iam)
    channel.web_proxy_auth_enabled = False
    try:
        async with await _asgi_client(channel) as client:
            resp = await client.get("/openclaw/?user_id=test1")
        assert resp.status_code == 200
        assert captured["user_id"] == "test1"
        set_cookie = resp.headers.get("set-cookie") or ""
        assert WEB_PROXY_COOKIE_NAME not in set_cookie
        assert WEB_PROXY_USER_COOKIE in set_cookie
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
