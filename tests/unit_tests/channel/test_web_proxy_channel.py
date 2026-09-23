# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for per-agent_type 3rd-agent Web proxy ports."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp
import httpx
import pytest
from aiohttp import web

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    DEFAULT_PORT_BASE,
    DEFAULT_PORT_SPAN,
    WEB_PROXY_COOKIE_NAME,
    WEB_PROXY_USER_COOKIE,
    WebProxyChannelConfig,
    _append_tail,
    _forward_headers,
    _forward_ws_headers,
    _retry_after_seconds,
    _sandbox_creating_response,
    _ensure_http_proxy_session,
    _ensure_proxy_session,
)
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_listen import (
    build_agent_port_app,
    handle_3rdagent_web,
)
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_range(span: int) -> int:
    span = max(1, span)
    for base in range(20000, 50000):
        socks: list[socket.socket] = []
        try:
            for offset in range(span):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", base + offset))
                socks.append(sock)
            return base
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
    raise RuntimeError("no free port range")


def test_web_proxy_config_from_dict_defaults() -> None:
    cfg = WebProxyChannelConfig.from_dict(None)
    assert cfg.enabled is False
    assert cfg.auth_enabled is True
    assert cfg.port_base == DEFAULT_PORT_BASE == 19101
    assert cfg.port_span == DEFAULT_PORT_SPAN == 200
    assert cfg.idle_timeout_sec == 900
    cfg = WebProxyChannelConfig.from_dict({"enabled": True, "listen_port": 19111})
    assert cfg.enabled is True
    assert cfg.auth_enabled is True
    cfg = WebProxyChannelConfig.from_dict(
        {"enabled": True, "auth_enabled": False, "port_base": 21000, "port_span": 4}
    )
    assert cfg.auth_enabled is False
    assert cfg.port_base == 21000
    assert cfg.port_span == 4


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
    assert _append_tail("http://127.0.0.1:8080/", "") == "http://127.0.0.1:8080/"


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


def test_forward_ws_headers_keeps_origin_and_strips_ws_keys(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_HOST", "172.31.12.19")
    monkeypatch.setenv("WEB_PORT", "19000")
    out = _forward_ws_headers(
        {
            "Origin": "http://1.95.65.197:19107",
            "Sec-WebSocket-Key": "abc",
            "Sec-WebSocket-Version": "13",
            "Cookie": "user_id=test1",
        },
        client_host="8.8.8.8",
        user_id="test1",
    )
    assert out["Origin"] == "http://1.95.65.197:19107"
    assert "Sec-WebSocket-Key" not in out
    assert "Cookie" not in out


def test_sandbox_creating_response_sets_retry_after() -> None:
    class _Busy(RuntimeError):
        retry_after_seconds = 5

    exc = _Busy("creating")
    assert _retry_after_seconds(exc) == 5
    resp = _sandbox_creating_response(exc)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"


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
    auth_enabled: bool = True,
) -> WebChannel:
    channel = WebChannel(
        WebChannelConfig(enabled=True, host="127.0.0.1", port=19000),
        RobotMessageRouter(),
    )
    channel.web_resolver = resolver
    channel.web_runtime_release = releaser
    channel.web_proxy_enabled = enabled
    channel.web_proxy_auth_enabled = auth_enabled
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=enabled,
        auth_enabled=auth_enabled,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        idle_timeout_sec=900,
    )
    if iam is not None:
        channel.agent_client = iam
    return channel


async def _port_client(channel: WebChannel, agent_type: str = "openclaw") -> httpx.AsyncClient:
    app = build_agent_port_app(channel, agent_type)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)


async def _close_proxy_session(channel: WebChannel) -> None:
    for attr in ("web_proxy_session", "web_proxy_http_session"):
        session = getattr(channel, attr, None)
        if session is not None and not getattr(session, "closed", True):
            await session.close()
            setattr(channel, attr, None)


async def _upstream(handler) -> tuple[web.AppRunner, int]:
    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, int(site._server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_ensure_proxy_session_single_flight_under_concurrency() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None)
    sessions = await asyncio.gather(*[_ensure_proxy_session(channel) for _ in range(8)])
    try:
        assert len({id(item) for item in sessions}) == 1
    finally:
        await sessions[0].close()


@pytest.mark.asyncio
async def test_http_proxy_session_force_closes_and_stays_off_ws_session() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None)
    http_sessions = await asyncio.gather(*[_ensure_http_proxy_session(channel) for _ in range(8)])
    ws_session = await _ensure_proxy_session(channel)
    try:
        assert len({id(item) for item in http_sessions}) == 1
        http_session = http_sessions[0]
        assert http_session is not ws_session
        assert http_session.connector.force_close is True
        assert ws_session.connector.force_close is False
    finally:
        await http_sessions[0].close()
        await ws_session.close()


@pytest.mark.asyncio
async def test_webchannel_does_not_mount_agent_prefix() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None)
    app = build_web_channel_app(channel)
    paths = {getattr(route, "path", None) for route in app.router.routes}
    assert "/ws" in paths
    assert "/{agent_type}" not in paths
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/openclaw/?user_id=u1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_port_http_forwards_root_and_assets_without_rewrite() -> None:
    html = (
        '<html data-openclaw-control-ui-base-path="">'
        '<script src="/assets/app.js"></script>ok</html>'
    )
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["path"] = request.path
        captured["auth"] = request.headers.get("Authorization", "")
        if request.path == "/assets/app.js":
            return web.Response(text="asset-ok", content_type="application/javascript")
        return web.Response(text=html, content_type="text/html")

    runner, upstream_port = await _upstream(upstream_handler)

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        captured["user_id"] = user_id
        captured["agent_type"] = agent_type
        captured["protocol"] = protocol
        return f"http://127.0.0.1:{upstream_port}"

    releases: list[tuple[str, str]] = []

    async def releaser(user_id: str, agent_type: str) -> None:
        releases.append((user_id, agent_type))

    channel = _proxy_channel(resolver=resolver, releaser=releaser, auth_enabled=False)
    try:
        async with await _port_client(channel) as client:
            index = await client.get("/?user_id=u1")
            asset = await client.get(
                "/assets/app.js",
                headers={"Cookie": f"{WEB_PROXY_USER_COOKIE}=u1"},
            )
        assert index.status_code == 200
        assert 'src="/assets/app.js"' in index.text
        assert "/openclaw/" not in index.text
        assert asset.status_code == 200
        assert asset.text == "asset-ok"
        assert captured["agent_type"] == "openclaw"
        assert captured["user_id"] == "u1"
        assert captured["path"] == "/assets/app.js"
        assert captured["auth"] == ""
        assert ("u1", "openclaw") in releases
        http_session = channel.web_proxy_http_session
        assert http_session is not None and http_session.connector.force_close is True
        assert channel.web_proxy_session is None
    finally:
        await _close_proxy_session(channel)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_port_auth_query_token_redirects_and_cookie_follows() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["cookie"] = request.headers.get("Cookie", "")
        captured["path"] = request.path
        return web.Response(text="ui-ok")

    runner, upstream_port = await _upstream(upstream_handler)

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        captured["user_id"] = user_id
        del agent_type, protocol
        return f"http://127.0.0.1:{upstream_port}"

    iam = _FakeIamClient(username="alice")
    channel = _proxy_channel(resolver=resolver, iam=iam)
    try:
        async with await _port_client(channel) as client:
            first = await client.get("/?user_id=alice&token=good")
            assert first.status_code == 302
            assert first.headers["location"] == "/?user_id=alice"
            assert "token=" not in first.headers["location"]
            set_cookie = first.headers.get("set-cookie") or ""
            assert WEB_PROXY_COOKIE_NAME in set_cookie
            assert WEB_PROXY_USER_COOKIE in set_cookie
            cookie = set_cookie.split(";", 1)[0]
            # httpx merges multiple set-cookie poorly; send both explicitly.
            second = await client.get(
                "/assets/app.js",
                headers={"Cookie": f"access_token=good; user_id=alice"},
            )
        assert second.status_code == 200
        assert captured["user_id"] == "alice"
        assert captured["path"] == "/assets/app.js"
        assert captured["cookie"] == ""
        assert iam.calls[-1]["token"] == "good"
        del cookie
    finally:
        await _close_proxy_session(channel)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_port_auth_user_mismatch_returns_403() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        raise AssertionError("resolver should not run")

    iam = _FakeIamClient()
    channel = _proxy_channel(resolver=resolver, iam=iam)
    async with await _port_client(channel) as client:
        resp = await client.get("/?user_id=other&token=good")
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_MISMATCH"


@pytest.mark.asyncio
async def test_port_missing_user_id_returns_400() -> None:
    channel = _proxy_channel(resolver=lambda *_a, **_k: None, auth_enabled=False)
    async with await _port_client(channel) as client:
        resp = await client.get("/")
    assert resp.status_code == 400


class _Creating(RuntimeError):
    retry_after_seconds = 5


class _FakeWs:
    def __init__(self, path: str) -> None:
        self.path = path
        self.request_headers: dict[str, str] = {}


def _capture_send(channel: WebChannel) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _send(ws, req_id, *, ok, payload=None, error=None, code=None):
        del ws, req_id
        sent.append({"ok": ok, "payload": payload or {}, "error": error, "code": code})

    channel.send_response = _send  # type: ignore[method-assign]
    return sent


@pytest.mark.asyncio
async def test_3rdagent_web_same_type_shares_port_and_url_has_user_and_token() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        assert protocol == "http"
        assert agent_type == "openclaw"
        return f"http://upstream/{user_id}"

    base = _free_range(2)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=False,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        port_base=base,
        port_span=2,
        idle_timeout_sec=900,
    )
    sent_a = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel,
            _FakeWs("/ws?user_id=alice"),
            "1",
            {"agent_type": "openclaw"},
            user_id="alice",
        )
        first = sent_a[-1]
        assert first["ok"] is True
        assert first["payload"]["port"] == base
        assert first["payload"]["url"] == f"http://127.0.0.1:{base}/?user_id=alice"
        assert "/openclaw" not in first["payload"]["url"]

        sent_b: list[dict[str, Any]] = []

        async def _send_b(ws, req_id, *, ok, payload=None, error=None, code=None):
            del ws, req_id
            sent_b.append({"ok": ok, "payload": payload or {}, "error": error, "code": code})

        channel.send_response = _send_b  # type: ignore[method-assign]
        await handle_3rdagent_web(
            channel,
            _FakeWs("/ws?token=good&user_id=bob"),
            "2",
            {"agent_type": "openclaw"},
            user_id="bob",
        )
        assert sent_b[-1]["payload"]["port"] == first["payload"]["port"]
        assert "user_id=bob" in sent_b[-1]["payload"]["url"]
        assert "token=" not in sent_b[-1]["payload"]["url"]
    finally:
        manager = channel.web_port_manager
        if manager is not None:
            await manager.close_all()


@pytest.mark.asyncio
async def test_3rdagent_web_different_types_get_different_ports() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    base = _free_range(2)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=False,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        port_base=base,
        port_span=2,
    )
    sent = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="alice"
        )
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "2", {"agent_type": "claude-code"}, user_id="alice"
        )
        ports = [item["payload"]["port"] for item in sent if item["ok"]]
        assert ports == [base, base + 1]
    finally:
        if channel.web_port_manager is not None:
            await channel.web_port_manager.close_all()


@pytest.mark.asyncio
async def test_3rdagent_web_builtin_and_creating_and_disabled() -> None:
    calls = {"n": 0}

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, protocol
        calls["n"] += 1
        if agent_type == "busy":
            raise _Creating("creating")
        return None

    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    sent = _capture_send(channel)
    await handle_3rdagent_web(
        channel, _FakeWs("/ws"), "1", {"agent_type": "JiuwenSwarm"}, user_id="alice"
    )
    await handle_3rdagent_web(channel, _FakeWs("/ws"), "2", {}, user_id="alice")
    await handle_3rdagent_web(
        channel, _FakeWs("/ws?token=good"), "3", {"agent_type": "busy"}, user_id="alice"
    )
    channel.web_proxy_enabled = False
    await handle_3rdagent_web(
        channel, _FakeWs("/ws"), "4", {"agent_type": "openclaw"}, user_id="alice"
    )
    assert [item["code"] for item in sent] == [
        "NO_WEB_ENDPOINT",
        "BAD_REQUEST",
        "AGENT_CREATING",
        "WEB_PROXY_DISABLED",
    ]
    assert "url" not in sent[2]["payload"]
    assert sent[2]["payload"]["retry_after_sec"] == 5
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_3rdagent_web_auth_puts_token_in_url_not_log_fields() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, auth_enabled=True)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=True,
        listen_host="127.0.0.1",
        advertise_host="172.31.12.19",
        port_base=base,
        port_span=1,
    )
    sent = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel,
            _FakeWs("/ws?token=secret-token&user_id=alice"),
            "1",
            {"agent_type": "openclaw"},
            user_id="alice",
        )
        payload = sent[-1]["payload"]
        assert payload["host"] == "172.31.12.19"
        assert payload["url"].startswith(f"http://172.31.12.19:{base}/?")
        assert "user_id=alice" in payload["url"]
        assert "token=secret-token" in payload["url"]
        assert "instance" not in payload["url"]
    finally:
        if channel.web_port_manager is not None:
            await channel.web_port_manager.close_all()


@pytest.mark.asyncio
async def test_port_ws_proxy_pumps_text() -> None:
    async def upstream_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{msg.data}")
            else:
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
        assert user_id == "u1"
        assert agent_type == "openclaw"
        assert protocol in {"http", "ws"}
        return f"http://127.0.0.1:{upstream_port}"

    releases: list[tuple[str, str]] = []

    async def releaser(user_id: str, agent_type: str) -> None:
        releases.append((user_id, agent_type))

    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, releaser=releaser, auth_enabled=False)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=False,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        port_base=base,
        port_span=1,
    )
    sent = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="u1"
        )
        port = sent[-1]["payload"]["port"]
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"http://127.0.0.1:{port}/ws?user_id=u1",
                headers={"Origin": "http://127.0.0.1:19107"},
            ) as ws:
                await ws.send_str("ping")
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.TEXT
                assert msg.data == "echo:ping"
        deadline = asyncio.get_running_loop().time() + 1.0
        while not releases and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert releases == [("u1", "openclaw")]
    finally:
        if channel.web_port_manager is not None:
            await channel.web_port_manager.close_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_3rdagent_web_creating_does_not_allocate_and_pool_exhausts() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, protocol
        if agent_type == "busy":
            raise _Creating("creating")
        return "http://upstream/"

    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=False,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        port_base=base,
        port_span=1,
    )
    sent = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "busy"}, user_id="alice"
        )
        assert sent[-1]["code"] == "AGENT_CREATING"
        assert channel.web_port_manager is None

        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "2", {"agent_type": "openclaw"}, user_id="alice"
        )
        assert sent[-1]["payload"]["port"] == base

        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "3", {"agent_type": "claude-code"}, user_id="alice"
        )
        assert sent[-1]["code"] == "PORT_POOL_EXHAUSTED"
        assert channel.web_port_manager is not None
        assert channel.web_port_manager.port_for("openclaw") == base
        assert channel.web_port_manager.port_for("claude-code") is None
    finally:
        if channel.web_port_manager is not None:
            await channel.web_port_manager.close_all()


@pytest.mark.asyncio
async def test_idle_reap_keeps_inflight_and_drops_quiet_listener() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    channel.web_proxy_config = WebProxyChannelConfig(
        enabled=True,
        auth_enabled=False,
        listen_host="127.0.0.1",
        advertise_host="127.0.0.1",
        port_base=base,
        port_span=1,
        idle_timeout_sec=1,
    )
    sent = _capture_send(channel)
    try:
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="alice"
        )
        manager = channel.web_port_manager
        assert manager is not None
        binding = manager._bindings["openclaw"]
        binding.inflight = 1
        binding.last_active = 0
        assert await manager.reap_idle(now=10_000) == []
        assert manager.port_for("openclaw") == sent[-1]["payload"]["port"]
        binding.inflight = 0
        assert await manager.reap_idle(now=10_000) == ["openclaw"]
        assert manager.port_for("openclaw") is None
    finally:
        if channel.web_port_manager is not None:
            await channel.web_port_manager.close_all()
