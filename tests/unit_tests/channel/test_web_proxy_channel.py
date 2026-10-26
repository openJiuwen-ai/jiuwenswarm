# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for per-agent_type 3rd-agent Web proxy ports."""

from __future__ import annotations

import asyncio
import gzip
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import httpx
import pytest
from aiohttp import web
from yarl import URL

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    WEB_PROXY_COOKIE_NAME,
    WEB_PROXY_USER_COOKIE,
    WebProxyChannelConfig,
    new_web_proxy_client_session,
    _append_tail,
    _filter_response_headers,
    _final_response_headers,
    _forward_headers,
    _forward_ws_headers,
    _is_loopback_host,
    _merge_request_query,
    _retry_after_seconds,
    _sandbox_creating_response,
    _TEST_NET_1,
    _ensure_http_proxy_session,
    _ensure_proxy_session,
)
from gateway.channel_manager.protocol.web_proxy import (
    web_proxy_listen,
)
from gateway.channel_manager.protocol.web_proxy.web_proxy_listen import (
    WebPortManager,
    _manager_for,
    _request_target_summary,
    build_agent_port_app,
    handle_3rdagent_web,
)
from gateway.channel_manager.web.web_channel_app import build_web_channel_app
from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


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
    assert cfg.idle_timeout_sec == 900
    cfg = WebProxyChannelConfig.from_dict({"enabled": True, "listen_port": 19111})
    assert cfg.enabled is True
    assert cfg.auth_enabled is True
    cfg = WebProxyChannelConfig.from_dict(
        {"enabled": True, "auth_enabled": False}
    )
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
    assert _append_tail("http://127.0.0.1:8080/", "") == "http://127.0.0.1:8080/"


def test_merge_request_query_keeps_route_and_business_params() -> None:
    url = (
        "http://yr:8888/serverless/v1/http/api/upload"
        "?instance=x&tenant_id=default&port=3080"
    )
    assert _merge_request_query(url, "sessionId=s1&name=problem.md") == (
        "http://yr:8888/serverless/v1/http/api/upload"
        "?instance=x&tenant_id=default&port=3080&sessionId=s1&name=problem.md"
    )


def test_merge_request_query_drops_route_and_iam_keys() -> None:
    url = "http://yr:8888/serverless/v1/http?instance=x&tenant_id=default&port=3080"
    merged = _merge_request_query(
        url,
        "sessionId=s1&port=9999&instance=evil&tenant_id=evil"
        "&token=secret&access_token=secret&refresh_token=secret"
        "&user_id=evil&agent_type=evil",
    )
    # Only the business param survives; route/IAM keys never reach upstream.
    assert merged == (
        "http://yr:8888/serverless/v1/http"
        "?instance=x&tenant_id=default&port=3080&sessionId=s1"
    )


def test_merge_request_query_preserves_blank_duplicates_and_utf8() -> None:
    url = "http://yr:8888/serverless/v1/http?instance=x&port=3080"
    merged = _merge_request_query(
        url, "tag=a&tag=b&empty=&name=%E9%97%AE%E9%A2%98.md"
    )
    assert "tag=a&tag=b" in merged
    assert "empty=" in merged
    assert "name=%E9%97%AE%E9%A2%98.md" in merged


def test_merge_request_query_noop_without_business_query() -> None:
    url = "http://yr:8888/serverless/v1/http?instance=x&port=3080"
    assert _merge_request_query(url, "") == url
    assert _merge_request_query(url, "token=secret&port=1") == url


def test_request_target_summary_keeps_bundle_payload_out_of_logs() -> None:
    tail = "__agentbox_plugins_/" + "A" * 4000
    upstream = (
        f"http://yr:8888/serverless/v1/http/{tail}"
        "?instance=x&port=3080&sessionId=s1"
    )
    kind, path_length, query_length, digest = _request_target_summary(
        tail, upstream
    )
    assert kind == "plugin_bundle"
    assert path_length == len(tail)
    assert query_length == len("instance=x&port=3080&sessionId=s1")
    assert len(digest) == 12
    assert tail not in digest


def test_request_target_summary_regular_path() -> None:
    kind, path_length, query_length, digest = _request_target_summary(
        "api/foo", "http://yr:8888/serverless/v1/http/api/foo?instance=x"
    )
    assert kind == "regular"
    assert path_length == len("api/foo")
    assert query_length == len("instance=x")
    assert len(digest) == 12


def test_forward_headers_sets_non_loopback_xff_and_strips_iam(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    out = _forward_headers(
        {
            "Host": "1.95.65.197:19101",
            "Authorization": "Bearer secret",
            "Cookie": "access_token=x; dsh_session=abc",
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
    assert "X-User-Id" not in out
    assert out["Accept"] == "text/html"
    # Host is preserved so agent Host/Origin checks (e.g. DSH) pass.
    assert out["Host"] == "1.95.65.197:19101"
    # IAM cookie dropped, the agent's own session cookie kept.
    assert out["Cookie"] == "dsh_session=abc"


def test_forward_headers_drops_iam_only_cookie_entirely(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    out = _forward_headers(
        {"Cookie": "access_token=x; refresh_token=y; user_id=u1"},
        client_host="172.31.12.19",
        user_id="u1",
    )
    assert "Cookie" not in out


def test_is_loopback_host_covers_v4_v6_and_mapped() -> None:
    for value in ("", "localhost", "127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
        assert _is_loopback_host(value) is True, value
    for value in ("0.0.0.0", "8.8.8.8", "example.local", "172.31.12.19"):
        assert _is_loopback_host(value) is False, value


def test_forward_headers_same_host_gateway_prepends_probe_hop(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_HOST", "172.31.12.19")
    out = _forward_headers({}, client_host="172.31.12.19", user_id="u1")
    # Same-host gateway traffic gets a synthetic first hop so the downstream
    # agent's trusted-proxy check sees a non-loopback, non-gateway client.
    assert out["X-Forwarded-For"] == f"{_TEST_NET_1}, 172.31.12.19"
    # A genuine remote peer is passed through unchanged (no synthetic hop).
    remote = _forward_headers({}, client_host="203.0.113.7", user_id="u1")
    assert remote["X-Forwarded-For"] == "203.0.113.7"


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


async def _listen(channel: WebChannel, desired: dict[str, int]) -> WebPortManager:
    """Bind the given ``{agent_type: port}`` map (as registry_watcher would)."""
    manager = _manager_for(channel)
    await manager.reconcile(desired)
    return manager


async def _close_manager(channel: WebChannel) -> None:
    manager = getattr(channel, "web_port_manager", None)
    if manager is not None:
        await manager.close_all()


async def _upstream(handler) -> tuple[web.AppRunner, int]:
    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    return runner, int(site._server.sockets[0].getsockname()[1])


@dataclass
class _ProxyUpstream:
    """One proxy fetch wired to a real aiohttp upstream for e2e coverage."""

    client: httpx.AsyncClient
    upstream_port: int
    resolver_calls: list[tuple[str, str, str]] = field(default_factory=list)
    releases: list[tuple[str, str]] = field(default_factory=list)
    channel: WebChannel = None  # type: ignore[assignment]


@asynccontextmanager
async def _proxied_upstream(handler, *, resolver=None, with_releaser: bool = False):
    """Start an upstream + channel + port client; clean everything up after.

    *resolver* overrides the default ``http://127.0.0.1:<port>`` resolver; it
    takes ``(user_id, agent_type, protocol, upstream_port)`` (e.g. the YuanRong
    tunnel form with route query). Yields :class:`_ProxyUpstream`.
    """
    runner, upstream_port = await _upstream(handler)
    resolver_calls: list[tuple[str, str, str]] = []
    if resolver is None:

        async def resolver(user_id, agent_type, protocol, _port=upstream_port):
            resolver_calls.append((user_id, agent_type, protocol))
            return f"http://127.0.0.1:{_port}"

    async def _resolver_adapter(user_id, agent_type, protocol):
        return await resolver(user_id, agent_type, protocol, upstream_port)

    releases: list[tuple[str, str]] = []

    async def releaser(user_id: str, agent_type: str) -> None:
        releases.append((user_id, agent_type))

    channel = _proxy_channel(
        resolver=_resolver_adapter,
        releaser=releaser if with_releaser else None,
        auth_enabled=False,
    )
    try:
        async with await _port_client(channel) as client:
            yield _ProxyUpstream(
                client=client,
                upstream_port=upstream_port,
                resolver_calls=resolver_calls,
                releases=releases,
                channel=channel,
            )
    finally:
        await _close_proxy_session(channel)
        await runner.cleanup()


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
        assert http_session.timeout.total is None
        assert ws_session.timeout.total is None
        assert http_session.timeout.connect == 30
        assert http_session.timeout.sock_connect == 30
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

    async with _proxied_upstream(upstream_handler, with_releaser=True) as ctx:
        index = await ctx.client.get("/?user_id=u1")
        asset = await ctx.client.get(
            "/assets/app.js",
            headers={"Cookie": f"{WEB_PROXY_USER_COOKIE}=u1"},
        )
        assert index.status_code == 200
        assert 'src="/assets/app.js"' in index.text
        assert "/openclaw/" not in index.text
        assert asset.status_code == 200
        assert asset.text == "asset-ok"
        assert captured["path"] == "/assets/app.js"
        assert captured["auth"] == ""
        assert ctx.resolver_calls and all(
            call == ("u1", "openclaw", "http") for call in ctx.resolver_calls
        )
        assert ("u1", "openclaw") in ctx.releases
        http_session = ctx.channel.web_proxy_http_session
        assert http_session is not None and http_session.connector.force_close is True
        assert ctx.channel.web_proxy_session is None


@pytest.mark.asyncio
async def test_http_proxy_logs_bundle_summary_not_full_path(monkeypatch) -> None:
    tail = "__agentbox_plugins_/" + "A" * 4000

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    messages: list[str] = []

    def _capture_info(message, *args, **kwargs) -> None:
        messages.append(message % args if args else message)

    monkeypatch.setattr(web_proxy_listen.logger, "info", _capture_info)
    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get(f"/{tail}?user_id=u1")
        assert resp.status_code == 200

    assert messages
    log_text = "\n".join(messages)
    assert tail not in log_text
    assert "target_kind=plugin_bundle" in log_text
    assert f"path_len={len(tail)}" in log_text
    assert "target_digest=" in log_text


@pytest.mark.asyncio
async def test_port_http_returns_upstream_redirect_without_following() -> None:
    upstream_paths: list[str] = []

    async def upstream_handler(request: web.Request) -> web.Response:
        upstream_paths.append(request.path)
        return web.Response(status=302, headers={"Location": "/login"})

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get("/old?user_id=u1")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    assert upstream_paths == ["/old"]


@pytest.mark.asyncio
async def test_http_proxy_upstream_header_timeout_releases(monkeypatch) -> None:
    async def upstream_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="late")

    monkeypatch.setattr(
        web_proxy_listen, "_UPSTREAM_HEADER_TIMEOUT_SECONDS", 0.1
    )
    async with _proxied_upstream(
        upstream_handler, with_releaser=True
    ) as ctx:
        resp = await ctx.client.get("/?user_id=u1")
        assert resp.status_code == 504
        deadline = asyncio.get_running_loop().time() + 1.0
        while not ctx.releases and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert ("u1", "openclaw") in ctx.releases


@pytest.mark.asyncio
async def test_http_proxy_upstream_body_timeout_releases(monkeypatch) -> None:
    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/plain"})
        await response.prepare(request)
        await asyncio.sleep(2)
        try:
            await response.write(b"late")
        except Exception:
            pass
        return response

    monkeypatch.setattr(
        web_proxy_listen, "_UPSTREAM_BODY_TIMEOUT_SECONDS", 0.1
    )
    async with _proxied_upstream(
        upstream_handler, with_releaser=True
    ) as ctx:
        resp = await ctx.client.get("/?user_id=u1")
        assert resp.status_code == 504
        deadline = asyncio.get_running_loop().time() + 1.0
        while not ctx.releases and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert ("u1", "openclaw") in ctx.releases


@pytest.mark.asyncio
async def test_port_http_forwards_business_query_and_streams_body() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["path"] = request.path
        captured["query"] = dict(request.query)
        captured["body"] = await request.read()
        captured["content_length"] = request.headers.get("Content-Length", "")
        return web.Response(text="ok")

    async def resolver(user_id: str, agent_type: str, protocol: str, upstream_port: int) -> str | None:
        return (
            f"http://127.0.0.1:{upstream_port}"
            "/serverless/v1/http?instance=x&tenant_id=default&port=3080"
        )

    async with (
        _proxied_upstream(
            upstream_handler, resolver=resolver, with_releaser=True
        ) as ctx
    ):
        resp = await ctx.client.post(
            "/api/session/uploadFileBinary"
            "?sessionId=sess-1&name=%E9%97%AE%E9%A2%98.md"
            "&port=9999&instance=evil&agent_type=evil",
            content=b"file-bytes",
            headers={"Cookie": f"{WEB_PROXY_USER_COOKIE}=u1"},
        )
        assert resp.status_code == 200
        assert resp.text == "ok"
        assert captured["path"] == "/serverless/v1/http/api/session/uploadFileBinary"
        # business query forwarded
        assert captured["query"]["sessionId"] == "sess-1"
        assert captured["query"]["name"] == "问题.md"
        # route params kept, browser override ignored
        assert captured["query"]["instance"] == "x"
        assert captured["query"]["tenant_id"] == "default"
        assert captured["query"]["port"] == "3080"
        assert "agent_type" not in captured["query"]
        # body streamed: no whole-file Content-Length, upstream still got it all
        assert captured["body"] == b"file-bytes"
        assert captured["content_length"] == ""
        assert ("u1", "openclaw") in ctx.releases


@pytest.mark.asyncio
async def test_port_http_preserves_multipart_content_type_boundary() -> None:
    boundary = "test-boundary-123"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
        '{"ok":true}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="bytes-0"; filename="blob"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + b"binary-data" + f"\r\n--{boundary}--\r\n".encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.post(
            "/api/workspaceFiles/readBytes",
            content=b"x",
            headers={"Cookie": f"{WEB_PROXY_USER_COOKIE}=u1"},
        )
        assert resp.status_code == 200
        # multipart boundary must survive the proxy's response rebuild
        assert resp.headers["content-type"] == (
            f"multipart/form-data; boundary={boundary}"
        )
        assert resp.content == body


@pytest.mark.asyncio
async def test_port_filters_iam_cookie_and_passes_upstream_set_cookie() -> None:
    captured: dict[str, Any] = {}

    async def upstream_handler(request: web.Request) -> web.Response:
        captured["host"] = request.headers.get("Host", "")
        captured["cookie"] = request.headers.get("Cookie", "")
        resp = web.Response(text="ok", content_type="text/html")
        resp.headers["Set-Cookie"] = "dsh_session=abc; Path=/; HttpOnly"
        return resp

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get(
            "/?user_id=u1",
            headers={
                "Host": "203.0.113.9:19101",
                "Cookie": "access_token=iam; dsh_session=abc",
            },
        )
        assert resp.status_code == 200
        # Browser Host is passed through (not rewritten to the upstream addr),
        # so an agent's Host/Origin check (e.g. DSH) sees the public authority.
        assert captured["host"] == "203.0.113.9:19101"
        # IAM cookie dropped before the sandbox; agent's own cookie kept.
        assert "access_token" not in captured["cookie"]
        assert "dsh_session=abc" in captured["cookie"]
        # Agent's own Set-Cookie is passed through to the browser.
        set_cookies = resp.headers.get_list("set-cookie")
        assert any("dsh_session=abc" in value for value in set_cookies)


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
    sent_a = _capture_send(channel)
    try:
        await _listen(channel, {"openclaw": base})
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
        await _close_manager(channel)


@pytest.mark.asyncio
async def test_3rdagent_web_different_types_get_different_ports() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    base = _free_range(2)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    sent = _capture_send(channel)
    try:
        await _listen(channel, {"openclaw": base, "claude-code": base + 1})
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="alice"
        )
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "2", {"agent_type": "claude-code"}, user_id="alice"
        )
        ports = [item["payload"]["port"] for item in sent if item["ok"]]
        assert ports == [base, base + 1]
    finally:
        await _close_manager(channel)


@pytest.mark.asyncio
async def test_3rdagent_web_builtin_and_creating_and_disabled() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, protocol
        if agent_type == "busy":
            raise _Creating("creating")
        return "http://upstream/"

    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    sent = _capture_send(channel)
    try:
        await _listen(channel, {"busy": base})
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
    finally:
        await _close_manager(channel)


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
    )
    sent = _capture_send(channel)
    try:
        await _listen(channel, {"openclaw": base})
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
        await _close_manager(channel)


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
    sent = _capture_send(channel)
    try:
        await _listen(channel, {"openclaw": base})
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="u1"
        )
        port = sent[-1]["payload"]["port"]
        assert port == base
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
        await _close_manager(channel)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ws_proxy_logs_bundle_summary_not_full_path(monkeypatch) -> None:
    tail = "__agentbox_plugins_/" + "A" * 4000

    async def upstream_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    runner, upstream_port = await _upstream(upstream_ws)

    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        assert user_id == "u1"
        assert agent_type == "openclaw"
        assert protocol in {"http", "ws"}
        return f"http://127.0.0.1:{upstream_port}"

    messages: list[str] = []

    def _capture_info(message, *args, **kwargs) -> None:
        messages.append(message % args if args else message)

    monkeypatch.setattr(web_proxy_listen.logger, "info", _capture_info)
    base = _free_range(1)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    sent = _capture_send(channel)
    try:
        await _listen(channel, {"openclaw": base})
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="u1"
        )
        port = sent[-1]["payload"]["port"]
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"http://127.0.0.1:{port}/{tail}?user_id=u1"
            ):
                pass
    finally:
        await _close_manager(channel)
        await runner.cleanup()

    log_text = "\n".join(messages)
    assert "[WebProxy] WS tunnel" in log_text
    assert tail not in log_text
    assert "target_kind=plugin_bundle" in log_text
    assert f"path_len={len(tail)}" in log_text
    assert "target_digest=" in log_text


@pytest.mark.asyncio
async def test_ws_proxy_upstream_handshake_timeout_releases(monkeypatch) -> None:
    async def upstream_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="not a websocket")

    runner, upstream_port = await _upstream(upstream_handler)

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
    sent = _capture_send(channel)
    monkeypatch.setattr(web_proxy_listen, "_WS_HANDSHAKE_TIMEOUT_SECONDS", 0.1)
    try:
        await _listen(channel, {"openclaw": base})
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="u1"
        )
        port = sent[-1]["payload"]["port"]
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientError):
                await session.ws_connect(f"http://127.0.0.1:{port}/ws?user_id=u1")
        deadline = asyncio.get_running_loop().time() + 1.0
        while not releases and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert ("u1", "openclaw") in releases
    finally:
        await _close_manager(channel)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_3rdagent_web_not_registered_and_not_listening() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    sent = _capture_send(channel)
    try:
        # Not in the registry's port map -> NO_WEB_ENDPOINT.
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "1", {"agent_type": "openclaw"}, user_id="alice"
        )
        assert sent[-1]["code"] == "NO_WEB_ENDPOINT"

        # Registered (desired) but not bound in this process yet.
        manager = _manager_for(channel)
        manager._desired = {"openclaw": 19107}
        await handle_3rdagent_web(
            channel, _FakeWs("/ws"), "2", {"agent_type": "openclaw"}, user_id="alice"
        )
        assert sent[-1]["code"] == "PORT_NOT_LISTENING"
        assert sent[-1]["payload"]["port"] == 19107
    finally:
        await _close_manager(channel)


@pytest.mark.asyncio
async def test_reconcile_binds_changes_and_removes_listener() -> None:
    async def resolver(user_id: str, agent_type: str, protocol: str) -> str | None:
        del user_id, agent_type, protocol
        return "http://upstream/"

    base = _free_range(2)
    channel = _proxy_channel(resolver=resolver, auth_enabled=False)
    try:
        manager = _manager_for(channel)

        await manager.reconcile({"openclaw": base})
        assert manager.port_for("openclaw") == base

        # Port change -> old listener closed, new one bound.
        await manager.reconcile({"openclaw": base + 1})
        assert manager.port_for("openclaw") == base + 1

        # Removal -> listener closed.
        await manager.reconcile({})
        assert manager.port_for("openclaw") is None
    finally:
        await _close_manager(channel)


@pytest.mark.asyncio
async def test_proxy_sessions_use_dummy_cookie_jar_no_cross_user_replay() -> None:
    """Shared proxy sessions must not store/replay upstream cookies across users."""
    session = new_web_proxy_client_session()
    http_session = new_web_proxy_client_session(force_close=True)
    try:
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        assert isinstance(http_session.cookie_jar, aiohttp.DummyCookieJar)
        assert http_session.connector.force_close is True

        # A cookie set by one user's sandbox must be neither stored nor replayed.
        url = URL("http://south.example/")
        session.cookie_jar.update_cookies({"dsh_auth": "ALICE_TOKEN"}, url)
        assert not session.cookie_jar.filter_cookies(url)
    finally:
        await session.close()
        await http_session.close()


def test_filter_response_headers_drops_hop_by_hop_and_connection_listed() -> None:
    raw = [
        (b"Content-Type", b"text/html"),
        (b"Connection", b"keep-alive, X-Custom-Hop"),
        (b"Keep-Alive", b"timeout=5"),
        (b"Transfer-Encoding", b"chunked"),
        (b"X-Custom-Hop", b"secret"),
        (b"ETag", b'W/"abc"'),
        (b"Set-Cookie", b"a=1"),
        (b"Set-Cookie", b"b=2"),
    ]
    out = _filter_response_headers(raw)
    names = [name for name, _ in out]
    assert (b"content-type", b"text/html") in out
    assert (b"etag", b'W/"abc"') in out
    assert names.count(b"set-cookie") == 2  # duplicate Set-Cookie survives
    for gone in (b"connection", b"keep-alive", b"transfer-encoding", b"x-custom-hop"):
        assert gone not in names


def test_final_response_headers_length_rules() -> None:
    upstream = [(b"content-type", b"application/octet-stream"), (b"content-length", b"999")]
    normal = _final_response_headers(upstream, method="GET", status=200, body=b"abcde")
    assert (b"content-length", b"5") in normal
    assert (b"content-length", b"999") not in normal

    head = _final_response_headers(upstream, method="HEAD", status=200, body=b"")
    assert (b"content-length", b"999") in head  # keep upstream size, no body

    for status in (204, 304):
        out = _final_response_headers(upstream, method="GET", status=status, body=b"")
        assert all(name != b"content-length" for name, _ in out)


@pytest.mark.asyncio
async def test_port_passes_content_disposition_and_cache_headers() -> None:
    disposition = (
        "attachment; filename=\"a.md\"; filename*=UTF-8''%E9%97%AE%E9%A2%98.md"
    )

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(
            text="data",
            content_type="text/markdown",
            headers={
                "Content-Disposition": disposition,
                "ETag": '"v1"',
                "Last-Modified": "Wed, 01 Oct 2025 00:00:00 GMT",
                "Cache-Control": "private, max-age=60",
                "Vary": "Accept-Encoding",
            },
        )

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get("/?user_id=u1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == disposition
        assert resp.headers["etag"] == '"v1"'
        assert resp.headers["cache-control"] == "private, max-age=60"
        assert resp.headers["vary"] == "Accept-Encoding"
        assert resp.text == "data"


@pytest.mark.asyncio
async def test_port_gzip_passthrough_keeps_encoding_and_length() -> None:
    raw = b"hello-gzip-" * 50
    compressed = gzip.compress(raw)

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(
            body=compressed,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/octet-stream",
            },
        )

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get("/?user_id=u1")
        assert resp.headers["content-encoding"] == "gzip"
        assert int(resp.headers["content-length"]) == len(compressed)  # matches bytes sent
        assert resp.content == raw  # decodes back to the original exactly once


@pytest.mark.asyncio
async def test_port_keeps_upstream_and_gateway_set_cookie() -> None:
    async def upstream_handler(request: web.Request) -> web.Response:
        resp = web.Response(text="ok", content_type="text/plain")
        resp.set_cookie("dsh_session", "abc", path="/")
        return resp

    async with _proxied_upstream(upstream_handler) as ctx:
        resp = await ctx.client.get("/?user_id=u1")
        cookies = resp.headers.get_list("set-cookie")
        assert any("dsh_session=abc" in item for item in cookies)  # upstream cookie kept
        assert len(cookies) >= 2  # gateway session cookie also present
