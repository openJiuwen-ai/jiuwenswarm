# coding: utf-8
"""企业级 MCP 双令牌端到端验证（Keycloak Client Credentials + 用户令牌）。

与 ``tests/unit_tests/common/test_mcp_dynamic_token_patch.py`` 的分工：

- 单测用 monkeypatch 替换 ``_request_token``，聚焦补丁内部状态机；
- 本文件**不替换**取 token 实现，而是运行两个真实 HTTP 服务：
  1. **Fake Keycloak**：按 ``application/x-www-form-urlencoded`` 校验
     ``grant_type=client_credentials`` + 实例凭据，签发短时效 actor token；
  2. **真实 MCP 网关**（streamable-http / SSE）：逐个 HTTP 请求校验
     ``Authorization``（用户授权）与 ``X-Actor-Token``（智能体负载授权），
     并把请求方身份回显到工具结果里。

覆盖的企业场景：
- 企业模板 ``data.custom_config`` → 实例凭据 → 条目 ``dynamic_token_url``
  → 适配器注册 mark / 真实 preflight → 真实 MCP 客户端连接与工具调用
  → 注销 unmark 的完整链路；
- 逐轮换用户、无令牌轮次回退静态、并发会话隔离、actor token 每请求刷新
  与过期、多网关各自 token 端点、token 服务故障降级 / 负缓存 / 恢复、
  SSE 重连换人、请求头注入防御、企业配置合并接线。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import socket
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.auth import auth_callback

from jiuwenswarm.enterprise.config import config_provider
from jiuwenswarm.common import mcp_dynamic_token_patch as dt
from jiuwenswarm.common.mcp_config import (
    build_mcp_server_config,
    preflight_mcp_server_reachable,
)
from jiuwenswarm.common.request_ext import lift_from_metadata, reset_ext


# ---------------------------------------------------------------------------
# 隔离：SDK registry 槽位 + 动态 token 模块态
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dynamic_token_state():
    saved = dict(auth_callback.AuthStrategyRegistry._strategies)
    yield
    auth_callback.AuthStrategyRegistry._strategies.clear()
    auth_callback.AuthStrategyRegistry._strategies.update(saved)
    dt._reset_dynamic_token_state_for_tests()


@contextlib.contextmanager
def _turn_ext(ext: dict[str, Any] | None):
    """模拟 AgentServer 入口：metadata['ext'] → 请求级 ContextVar。"""
    token = lift_from_metadata({"ext": dict(ext)} if ext else None)
    try:
        yield
    finally:
        reset_ext(token)


# ---------------------------------------------------------------------------
# Fake Keycloak：真实 HTTP、真实 form 校验、真实签发/过期
# ---------------------------------------------------------------------------


class TokenAuthority:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.mode = "ok"
        self.ttl = 300.0
        self.delay = 0.0
        self.fetches: list[dict[str, Any]] = []
        self.issued: dict[str, float] = {}
        self._seq = 0

    def issue(self, audience: str) -> str:
        self._seq += 1
        token = f"actor-{audience}-{self._seq}"
        self.issued[token] = time.monotonic() + self.ttl
        return token

    def validate(self, token: str, audience: str) -> bool:
        expires_at = self.issued.get(token)
        return (
            token.startswith(f"actor-{audience}-")
            and expires_at is not None
            and expires_at > time.monotonic()
        )


async def _capture_body(receive):
    """读完 POST body 并返回可重放的 receive。"""
    messages: list[dict[str, Any]] = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            break
        body += message.get("body", b"") or b""
        if not message.get("more_body", False):
            break

    async def replay():
        if messages:
            return messages.pop(0)
        return await receive()

    return body, replay


async def _respond(
    send,
    status: int,
    body: bytes,
    content_type: str = "application/json",
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_keycloak_app(authority: TokenAuthority):
    """``POST /token/<audience>``：Keycloak Client Credentials 形态。"""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body, _ = await _capture_body(receive)
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        audience = str(scope.get("path", "")).rstrip("/").rsplit("/", 1)[-1]
        authority.fetches.append(
            {"audience": audience, "form": dict(form), "at": time.monotonic()}
        )
        if authority.delay:
            await asyncio.sleep(authority.delay)
        try:
            mode = authority.mode
            if mode == "http_500":
                await _respond(send, 500, b'{"error":"server_error"}')
                return
            if mode == "html":
                await _respond(
                    send, 200, b"<html><body>proxy error</body></html>", "text/html"
                )
                return
            if mode == "empty_json":
                await _respond(send, 200, b"{}")
                return
            if mode == "nested_token":
                await _respond(send, 200, b'{"access_token": {"nested": "x"}}')
                return
            if (
                form.get("grant_type") != "client_credentials"
                or form.get("client_id") != authority.client_id
                or form.get("client_secret") != authority.client_secret
            ):
                await _respond(send, 401, b'{"error":"invalid_client"}')
                return
            token = authority.issue(audience)
            await _respond(
                send,
                200,
                json.dumps(
                    {
                        "access_token": token,
                        "token_type": "Bearer",
                        "expires_in": int(authority.ttl),
                    }
                ).encode(),
            )
        except Exception:  # noqa: BLE001 - 客户端超时断开后 ASGI send 会失败
            return

    return app


# ---------------------------------------------------------------------------
# 真实 MCP 网关：逐请求双令牌鉴权 + 身份回显
# ---------------------------------------------------------------------------

_GATEWAY_IDENTITY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "e2e_gateway_identity", default="anonymous"
)


def _headers(scope) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}


def _jsonrpc_method(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "POST"
    if isinstance(payload, dict):
        return str(payload.get("method") or "response")
    return "POST"


def _identity_from_authorization(value: str) -> str:
    if value.startswith("Bearer "):
        return value[len("Bearer ") :]
    return value or "anonymous"


def _current_request_identity() -> str:
    """工具执行期读取**本条 JSON-RPC 消息**对应的 HTTP 请求头。

    streamable-http 的会话处理跑在长生命周期任务里，外层 ASGI 包装写入的
    ContextVar 不会传播到工具任务；mcp 低层 server 在每条消息处理前设置
    ``request_ctx``（携带当前 ASGI request），这才是真实调用方身份。
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        request = getattr(request_ctx.get(), "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return _identity_from_authorization(headers.get("authorization", ""))
    except Exception:  # noqa: BLE001 - 测试网关：读不到回退包装层身份
        pass
    return _GATEWAY_IDENTITY.get()


class GatewayState:
    def __init__(
        self,
        name: str,
        authority: TokenAuthority,
        audience: str,
        *,
        accepted_users: set[str] | None = None,
        require_actor: bool = True,
    ) -> None:
        self.name = name
        self.authority = authority
        self.audience = audience
        self.accepted_users = accepted_users or {
            "Bearer user-A",
            "Bearer user-B",
            "Bearer static",
        }
        self.require_actor = require_actor
        self.records: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []

    def authorize(self, headers: dict[str, str]) -> tuple[bool, str, str]:
        authorization = headers.get("authorization") or ""
        actor = headers.get("x-actor-token")
        if authorization not in self.accepted_users:
            return False, f"user auth rejected: {authorization!r}", ""
        if self.require_actor:
            if not actor or not actor.startswith("Bearer "):
                return False, f"actor token missing: {actor!r}", ""
            token = actor[len("Bearer ") :]
            if not self.authority.validate(token, self.audience):
                return False, f"actor token invalid/expired: {actor!r}", ""
        return True, "", _identity_from_authorization(authorization)


def _record_gateway_request(
    state: GatewayState,
    *,
    scope,
    headers: dict[str, str],
    body: bytes,
    authorized: bool,
    reason: str,
    response_headers: dict[str, str],
) -> dict[str, Any]:
    record = {
        "gateway": state.name,
        "method": scope.get("method"),
        "path": scope.get("path"),
        "jsonrpc_method": _jsonrpc_method(body) if body else scope.get("method"),
        "authorization": headers.get("authorization"),
        "actor": headers.get("x-actor-token"),
        "session": headers.get("mcp-session-id", ""),
        "response_session": response_headers.get("mcp-session-id", ""),
        "authorized": authorized,
        "reason": reason,
    }
    state.records.append(record)
    if not authorized:
        state.rejections.append(record)
    return record


def build_streamable_gateway(state: GatewayState):
    """真实 FastMCP streamable-http 网关，外层做逐请求鉴权。"""
    from mcp.server.fastmcp import FastMCP

    gateway = FastMCP(state.name)

    @gateway.tool()
    def whoami() -> str:
        """返回网关从本次请求 Authorization 解析出的用户身份。"""
        return _current_request_identity()

    @gateway.tool()
    def add(a: int, b: int) -> int:
        return a + b

    inner = gateway.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = _headers(scope)
        body = b""
        replay = receive
        if scope.get("method") == "POST":
            body, replay = await _capture_body(receive)
        authorized, reason, identity = state.authorize(headers)
        response_headers: dict[str, str] = {}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                response_headers.update(
                    {
                        k.decode().lower(): v.decode()
                        for k, v in message.get("headers", [])
                    }
                )
            await send(message)

        record = _record_gateway_request(
            state,
            scope=scope,
            headers=headers,
            body=body,
            authorized=authorized,
            reason=reason,
            response_headers=response_headers,
        )
        if not authorized:
            await _respond(
                send,
                401,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32001, "message": reason},
                    }
                ).encode(),
            )
            return
        token = _GATEWAY_IDENTITY.set(identity)
        try:
            await inner(scope, replay, send_wrapper)
        finally:
            _GATEWAY_IDENTITY.reset(token)
        record["response_session"] = response_headers.get("mcp-session-id", "")

    return app


def build_sse_gateway(state: GatewayState):
    """真实低层 SSE MCP 网关，外层做逐请求鉴权。"""
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/messages/")
    server = Server(state.name)

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="whoami",
                description="return caller identity",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [types.TextContent(type="text", text=_current_request_identity())]

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (read, write):
            await server.run(read, write, server.create_initialization_options())

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        headers = _headers(scope)
        path = scope.get("path", "")
        body = b""
        replay = receive
        if scope.get("method") == "POST":
            body, replay = await _capture_body(receive)
        authorized, reason, identity = state.authorize(headers)
        _record_gateway_request(
            state,
            scope=scope,
            headers=headers,
            body=body,
            authorized=authorized,
            reason=reason,
            response_headers={},
        )
        if not authorized:
            await _respond(send, 401, b'{"error": "unauthorized"}')
            return
        token = _GATEWAY_IDENTITY.set(identity)
        try:
            if path == "/sse":
                await handle_sse(scope, replay, send)
                return
            if path.startswith("/messages"):
                await sse.handle_post_message(scope, replay, send)
                return
            await _respond(send, 404, b"not found", "text/plain")
        finally:
            _GATEWAY_IDENTITY.reset(token)

    return app


# ---------------------------------------------------------------------------
# 服务器/客户端辅助
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _serve(app: Any):
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn test server failed to start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:  # noqa: BLE001 - 关停失败不影响断言
            task.cancel()


def _entry(
    *,
    name: str,
    url: str,
    transport: str,
    dynamic_token_url: str | None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "server_id": name,
        "transport": transport,
        "url": url,
        "headers": headers or {"Authorization": "Bearer static"},
    }
    if dynamic_token_url:
        entry["dynamic_token_url"] = dynamic_token_url
    return entry


def _cfg(entry: dict[str, Any]) -> McpServerConfig:
    cfg = build_mcp_server_config(entry, server_id_scope="jiuwenswarm")
    assert cfg is not None, entry
    return cfg


def _enterprise(client_id: str, client_secret: str) -> Any:
    return SimpleNamespace(
        agent_template={
            "data": {
                "custom_config": [
                    {"client_id": client_id, "client_secret": client_secret}
                ]
            }
        },
        resource_id="e2e-bot",
        ref_template_id="e2e-template",
    )


def _enable_enterprise(
    client_id: str = "e2e-agent", client_secret: str = "e2e-secret"
) -> None:
    dt.apply_dispatched_credentials(_enterprise(client_id, client_secret))
    dt.apply_mcp_dynamic_token_patch()


@contextlib.contextmanager
def _turn(token: str | None):
    with _turn_ext({"X-User-Token": token} if token else None):
        yield


def _ok_result() -> SimpleNamespace:
    return SimpleNamespace(is_ok=lambda: True)


def _bare_adapter():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=SimpleNamespace(add=lambda _c: None))
    adapter._registered_mcp_server_ids = set()
    adapter._registered_mcp_servers = {}
    adapter._session_selected_mcp = set()
    return adapter


def _valid_actors(
    state: GatewayState, *, jsonrpc_method: str | None = None
) -> list[tuple[str, str]]:
    """返回 ``[(authorization, actor_token)]``（可限定 JSON-RPC 方法）。"""
    out: list[tuple[str, str]] = []
    for record in state.records:
        if not record["authorized"]:
            continue
        if jsonrpc_method and record["jsonrpc_method"] != jsonrpc_method:
            continue
        out.append((record["authorization"], record["actor"]))
    return out


def _last_tool_call(state: GatewayState) -> dict[str, Any]:
    calls = [
        record
        for record in state.records
        if record["jsonrpc_method"] == "tools/call" and record["authorized"]
    ]
    assert calls, "no authorized tools/call record"
    return calls[-1]


# ---------------------------------------------------------------------------
# 1. 企业配置 → 适配器注册 → 真实工具调用 → 注销
# ---------------------------------------------------------------------------


async def _scenario_full_chain():
    """企业链路全打通：模板凭据 → dynamic_token_url → mark/preflight →
    真实 MCP 连接调用 → unmark。"""
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from openjiuwen.core.runner import Runner

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState(
        "e2e-stream",
        authority,
        "aud-1",
        accepted_users={"Bearer user-A", "Bearer user-B"},
    )
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="e2e-stream",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-1",
        )
        cfg = _cfg(entry)
        adapter = _bare_adapter()

        added: list[Any] = []

        async def fake_add(server_cfg, *, tag=None):
            added.append(server_cfg)
            return _ok_result()

        async def fake_remove(server_id):
            return None

        mp = pytest.MonkeyPatch()
        mp.setattr(Runner.resource_mgr, "add_mcp_server", fake_add)
        mp.setattr(Runner.resource_mgr, "remove_mcp_server", fake_remove)

        with _turn("Bearer user-A"):
            registered = await adapter._register_mcp_server(
                cfg,
                tag="agent.main",
                dynamic_token_url=entry["dynamic_token_url"],
            )
        assert registered is True
        assert added and added[0] is cfg
        assert dt.is_dynamic_token_server("e2e-stream")
        assert dt._registry_token_url("e2e-stream") == entry["dynamic_token_url"]
        # 真实 preflight 已带用户令牌 + actor token 通过网关鉴权
        preflight = [r for r in state.records if r["jsonrpc_method"] == "initialize"]
        assert preflight and preflight[0]["authorization"] == "Bearer user-A"
        assert authority.validate(
            preflight[0]["actor"][len("Bearer ") :], "aud-1"
        )

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
            with _turn("Bearer user-A"):
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
            with _turn("Bearer user-B"):
                # 逐轮覆盖必须把 B 发出去，且不得沿用 A
                assert await client.call_tool("whoami", {}, timeout=10) == "user-B"
            with _turn("Bearer user-A"):
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
        finally:
            await client.disconnect(timeout=5)

        calls = _valid_actors(state, jsonrpc_method="tools/call")
        assert calls
        actors = [actor for _authorization, actor in calls]
        # 每个出网请求都实时取 token（唯一值），且全部由本网关的 token 端点签发
        assert len(set(actors)) == len(actors)
        assert all(authority.validate(a[len("Bearer ") :], "aud-1") for a in actors)
        assert {f["audience"] for f in authority.fetches} == {"aud-1"}

        await adapter._unregister_mcp_server("e2e-stream")
        assert not dt.is_dynamic_token_server("e2e-stream")
        assert dt._LATEST_PROVIDERS.get("e2e-stream") is None
        mp.undo()


# ---------------------------------------------------------------------------
# 2. 多轮用户切换 + 无令牌轮次
# ---------------------------------------------------------------------------


async def _scenario_multiturn():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState("gw-switch", authority, "aud-switch")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-switch",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-switch",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-switch", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
            with _turn("Bearer user-B"):
                assert await client.call_tool("whoami", {}, timeout=10) == "user-B"
            # 无令牌轮次：回退条目静态 Authorization，绝不沿用 B
            with _turn(None):
                assert await client.call_tool("whoami", {}, timeout=10) == "static"
            with _turn("Bearer user-A"):
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
        finally:
            await client.disconnect(timeout=5)

        tool_auths = [
            record["authorization"]
            for record in state.records
            if record["jsonrpc_method"] == "tools/call" and record["authorized"]
        ]
        assert tool_auths == [
            "Bearer user-A",
            "Bearer user-B",
            "Bearer static",
            "Bearer user-A",
        ]


# ---------------------------------------------------------------------------
# 3. 并发会话隔离（同网关、同 server_id、不同用户）
# ---------------------------------------------------------------------------


async def _scenario_concurrent():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState("gw-concurrent", authority, "aud-c")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-concurrent",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-c",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-concurrent", entry["dynamic_token_url"])

        client_a = StreamableHttpClient(cfg)
        client_b = StreamableHttpClient(cfg)

        # 每个会话由独立任务持有完整生命周期（connect/call/disconnect 同任务，
        # 与真实 AgentServer 的会话执行模型一致）；两个会话并发交替执行。
        results: dict[str, list[str]] = {"user-A": [], "user-B": []}

        async def session(client, label: str, user: str) -> None:
            with _turn(user):
                assert await client.connect(timeout=10) is True
            try:
                for _ in range(10):
                    # 尽量让两个会话的调用交错重叠
                    await asyncio.sleep(0)
                    with _turn(user):
                        results[label].append(
                            await client.call_tool("whoami", {}, timeout=10)
                        )
            finally:
                await client.disconnect(timeout=5)

        await asyncio.gather(
            session(client_a, "user-A", "Bearer user-A"),
            session(client_b, "user-B", "Bearer user-B"),
        )

        assert results["user-A"] == ["user-A"] * 10
        assert results["user-B"] == ["user-B"] * 10

        # 按 MCP 会话归集：每个会话的 Authorization 唯一且不串
        by_session: dict[str, set[str]] = {}
        for record in state.records:
            if not record["authorized"] or record["jsonrpc_method"] != "tools/call":
                continue
            session = record["session"]
            by_session.setdefault(session, set()).add(record["authorization"])
        assert len(by_session) == 2
        assert all(len(values) == 1 for values in by_session.values())
        assert {next(iter(v)) for v in by_session.values()} == {
            "Bearer user-A",
            "Bearer user-B",
        }


# ---------------------------------------------------------------------------
# 4. actor token 每请求实时刷新 + 短时效过期
# ---------------------------------------------------------------------------


async def _scenario_expiry():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    authority.ttl = 0.2
    state = GatewayState("gw-expiry", authority, "aud-exp")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-expiry",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-exp",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-expiry", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                first_actor = _last_tool_call(state)["actor"]
                first_token = first_actor[len("Bearer ") :]
                # 模拟短时效过期（不走真实 sleep，避免测试环境跨 loop 污染）
                authority.issued[first_token] = 0.0
                assert not authority.validate(first_token, "aud-exp")
                # 每请求实时取 → 第二个请求自动拿到新 token，调用不失败
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                second_actor = _last_tool_call(state)["actor"]
                assert second_actor != first_actor
                assert authority.validate(second_actor[len("Bearer ") :], "aud-exp")
        finally:
            await client.disconnect(timeout=5)
        dt.unmark_dynamic_token_server("gw-expiry")

        # 顺序工具调用各取各的 token（并发请求允许 single-flight 合并，
        # 因此不要求所有 HTTP 请求的 token 全局唯一）。
        tool_actors = [
            record["actor"]
            for record in state.records
            if record["jsonrpc_method"] == "tools/call" and record["authorized"]
        ]
        assert len(tool_actors) == 2
        assert len(set(tool_actors)) == 2


# ---------------------------------------------------------------------------
# 5. token 服务故障：不阻断请求 + 负缓存
# ---------------------------------------------------------------------------


async def _scenario_degrade():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    authority.mode = "http_500"
    # 网关允许空 actor（由网关决定放行/401），验证客户端侧"失败不阻断"
    state = GatewayState("gw-degrade", authority, "aud-deg", require_actor=False)
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-degrade",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-deg",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-degrade", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                fetches_after_connect = len(authority.fetches)
                assert fetches_after_connect <= 2  # single-flight / 负缓存
                for _ in range(5):
                    assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                # 负缓存窗口内不再打 token 服务
                assert len(authority.fetches) == fetches_after_connect
        finally:
            await client.disconnect(timeout=5)

        # 每个请求都照常发出，动态头为空字符串
        assert state.rejections == []
        assert all(record["actor"] == "" for record in state.records)
        assert all(record["authorization"] == "Bearer user-A" for record in state.records)
        tool_calls = [
            r for r in state.records if r["jsonrpc_method"] == "tools/call"
        ]
        assert len(tool_calls) == 5
        assert all(f["form"]["grant_type"] == "client_credentials" for f in authority.fetches)


async def _scenario_recover():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    authority.mode = "http_500"
    state = GatewayState("gw-recover", authority, "aud-rec", require_actor=False)
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-recover",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-rec",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-recover", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                assert _last_tool_call(state)["actor"] == ""

                authority.mode = "ok"
                # 直接把负缓存窗口拨到已结束（等价于窗口自然到期，避免真实 sleep）
                dt._LATEST_PROVIDERS["gw-recover"]._negative_until = 0.0
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                recovered = _last_tool_call(state)["actor"]
                assert recovered
                assert authority.validate(recovered[len("Bearer ") :], "aud-rec")
        finally:
            await client.disconnect(timeout=5)


async def _scenario_hard_failure():
    """网关要求 actor：token 服务 500 时建连应快速失败（返回 False），不挂死。"""
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    authority.mode = "http_500"
    state = GatewayState("gw-hard", authority, "aud-hard", require_actor=True)
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-hard",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-hard",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-hard", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)

        async def _attempt_connect():
            with _turn("Bearer user-A"):
                return await client.connect(timeout=10)

        started = time.monotonic()
        task = asyncio.create_task(_attempt_connect())
        try:
            connected = await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except BaseException:  # noqa: BLE001 - 建连失败（含 SDK cancel scope）即"未连上"
            connected = False
        elapsed = time.monotonic() - started
        with contextlib.suppress(BaseException):
            await client.disconnect(timeout=5)
        assert connected is False
        assert elapsed < 8  # token 5s 超时之内，而不是 MCP 30s 挂死
        assert state.rejections
        assert all(r["actor"] == "" for r in state.rejections)


async def _scenario_bad_responses(mode: str):
    """坏 token 响应（5xx / HTML / 空 JSON / 非字符串 access_token）统一降级。"""
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    authority.mode = mode
    state = GatewayState(f"gw-{mode}", authority, "aud-bad", require_actor=False)
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name=f"gw-{mode}",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-bad",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server(f"gw-{mode}", entry["dynamic_token_url"])
        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
        finally:
            await client.disconnect(timeout=5)
        assert all(r["actor"] == "" for r in state.records)


# ---------------------------------------------------------------------------
# 6. 多网关：各自 token 端点 / audience，不交叉复用
# ---------------------------------------------------------------------------


async def _scenario_multi_gateway():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state_1 = GatewayState("gw-one", authority, "aud-one")
    state_2 = GatewayState("gw-two", authority, "aud-two")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state_1)
    ) as url_1, _serve(build_streamable_gateway(state_2)) as url_2:
        _enable_enterprise()
        entry_1 = _entry(
            name="gw-one",
            url=url_1 + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-one",
        )
        entry_2 = _entry(
            name="gw-two",
            url=url_2 + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-two",
        )
        cfg_1, cfg_2 = _cfg(entry_1), _cfg(entry_2)
        assert dt.mark_dynamic_token_server("gw-one", entry_1["dynamic_token_url"])
        assert dt.mark_dynamic_token_server("gw-two", entry_2["dynamic_token_url"])

        # 两个网关的客户端各自在独立任务里持有完整生命周期：anyio cancel
        # scope 必须 LIFO 退出，不能在同一个任务里交错 connect/disconnect
        # 两个 SDK 客户端。
        results: list[str] = []

        async def gateway_session(cfg, label: str) -> None:
            client = StreamableHttpClient(cfg)
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
            try:
                with _turn("Bearer user-A"):
                    results.append(
                        f"{label}={await client.call_tool('whoami', {}, timeout=10)}"
                    )
            finally:
                await client.disconnect(timeout=5)

        await asyncio.gather(
            gateway_session(cfg_1, "one"), gateway_session(cfg_2, "two")
        )
        assert sorted(results) == ["one=user-A", "two=user-A"]

        # 每个网关收到的 actor token 只能由自己的 audience 端点签发
        for record in state_1.records:
            if not record["authorized"]:
                continue
            token = record["actor"][len("Bearer ") :]
            assert authority.validate(token, "aud-one")
            assert not authority.validate(token, "aud-two")
        for record in state_2.records:
            if not record["authorized"]:
                continue
            token = record["actor"][len("Bearer ") :]
            assert authority.validate(token, "aud-two")
            assert not authority.validate(token, "aud-one")
        assert {f["audience"] for f in authority.fetches} == {"aud-one", "aud-two"}


# ---------------------------------------------------------------------------
# 7. SSE：建连 / 重连换人 / 无令牌回退
# ---------------------------------------------------------------------------


async def _scenario_sse():
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState("gw-sse", authority, "aud-sse")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_sse_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-sse",
            url=gateway_url + "/sse",
            transport="sse",
            dynamic_token_url=keycloak_url + "/token/aud-sse",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-sse", entry["dynamic_token_url"])

        client = SseClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
            with _turn("Bearer user-B"):
                assert await client.reconnect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-B"
            with _turn(None):
                # 无令牌重连必须回退静态头，不能残留 B（owner task 冻结回归）
                assert await client.reconnect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "static"
        finally:
            await client.disconnect(timeout=5)

        get_sse = [r for r in state.records if r["path"] == "/sse"]
        assert [r["authorization"] for r in get_sse] == [
            "Bearer user-A",
            "Bearer user-B",
            "Bearer static",
        ]
        initialize = [
            r for r in state.records if r["jsonrpc_method"] == "initialize"
        ]
        assert [r["authorization"] for r in initialize] == [
            "Bearer user-A",
            "Bearer user-B",
            "Bearer static",
        ]
        # SSE 的每个请求（GET /sse、initialize、通知、tools/call）都带有效 actor
        assert all(
            authority.validate(r["actor"][len("Bearer ") :], "aud-sse")
            for r in state.records
            if r["authorized"]
        )


# ---------------------------------------------------------------------------
# 8. 注入防御：非法用户令牌不得进入请求头
# ---------------------------------------------------------------------------


async def _scenario_injection():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState("gw-inject", authority, "aud-inj")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-inject",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-inj",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-inject", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
            with _turn("Bearer user-A\r\nX-Evil: 1"):
                assert await client.call_tool("whoami", {}, timeout=10) == "static"
        finally:
            await client.disconnect(timeout=5)

        tool_calls = [
            r for r in state.records if r["jsonrpc_method"] == "tools/call"
        ]
        assert tool_calls[-1]["authorization"] == "Bearer static"
        assert all("x-evil" not in r for r in state.records)


# ---------------------------------------------------------------------------
# 9. preflight 与运行时同源
# ---------------------------------------------------------------------------


async def _scenario_preflight():
    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState(
        "gw-preflight",
        authority,
        "aud-pf",
        accepted_users={"Bearer user-A"},
    )
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-preflight",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-pf",
        )
        cfg = _cfg(entry)

        with _turn("Bearer user-A"):
            ok, reason = await preflight_mcp_server_reachable(
                cfg, dynamic_token_url=entry["dynamic_token_url"]
            )
        assert ok, reason
        assert state.records[-1]["authorization"] == "Bearer user-A"
        assert authority.validate(
            state.records[-1]["actor"][len("Bearer ") :], "aud-pf"
        )

        # 无用户令牌 → 静态头不被网关接受 → 探活失败（与真实调用一致）
        ok, reason = await preflight_mcp_server_reachable(
            cfg, dynamic_token_url=entry["dynamic_token_url"]
        )
        assert ok is False
        assert "401" in reason

        # token 服务失败不判不可达：网关放行空 actor 时仍可达
        authority.mode = "http_500"
        state_2 = GatewayState(
            "gw-preflight-2",
            authority,
            "aud-pf",
            accepted_users={"Bearer static"},
            require_actor=False,
        )
        async with _serve(build_streamable_gateway(state_2)) as gateway_url_2:
            entry_2 = _entry(
                name="gw-preflight-2",
                url=gateway_url_2 + "/mcp",
                transport="streamable-http",
                dynamic_token_url=keycloak_url + "/token/aud-pf",
            )
            ok, _ = await preflight_mcp_server_reachable(
                _cfg(entry_2), dynamic_token_url=entry_2["dynamic_token_url"]
            )
            assert ok is True
            assert state_2.records[-1]["actor"] == ""


# ---------------------------------------------------------------------------
# 10. 企业配置合并接线 / 凭据热更 / 注销逐出
# ---------------------------------------------------------------------------


def _scenario_config_wiring():
    import jiuwenswarm.agents.harness.common.memory.config as memory_config

    mp = pytest.MonkeyPatch()
    mp.setattr(
        config_provider,
        "apply_enterprise_models_and_mcp",
        lambda base, loaded: (base, False, False),
    )
    mp.setattr(
        memory_config,
        "merge_memory_config_into_config",
        lambda merged: merged,
    )

    merged = config_provider._apply_enterprise_policy(
        {"mcp": {"servers": []}}, _enterprise("agent-merge", "secret-merge")
    )
    assert merged == {"mcp": {"servers": []}}
    creds = dt.read_instance_credentials()
    assert creds is not None
    assert (creds.client_id, creds.client_secret) == ("agent-merge", "secret-merge")

    # 下一次合并不携带合法凭据对 → 清除（不能沿用旧实例身份）
    config_provider._apply_enterprise_policy(
        {}, SimpleNamespace(agent_template={"data": {"custom_config": []}})
    )
    assert dt.read_instance_credentials() is None
    mp.undo()


def _scenario_extract():
    enterprise = SimpleNamespace(
        agent_template={
            "data": {
                "custom_config": [
                    {"client_id": "", "client_secret": "x"},
                    "not-a-dict",
                    {"client_id": " agent-1 ", "client_secret": " secret-1 "},
                ]
            }
        }
    )
    assert dt.extract_dispatched_credentials(enterprise.agent_template) == {
        "client_id": "agent-1",
        "client_secret": "secret-1",
    }


async def _scenario_unregister():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState(
        "gw-evict",
        authority,
        "aud-ev",
        accepted_users={"Bearer user-A", "Bearer static"},
        # 注销后该网关按普通 MCP 服务工作（无 actor token）
        require_actor=False,
    )
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_streamable_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-evict",
            url=gateway_url + "/mcp",
            transport="streamable-http",
            dynamic_token_url=keycloak_url + "/token/aud-ev",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-evict", entry["dynamic_token_url"])

        client = StreamableHttpClient(cfg)
        with _turn("Bearer user-A"):
            assert await client.connect(timeout=10) is True
            assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
        assert any(p._server_id == "gw-evict" for p in dt._ACTIVE_PROVIDERS)

        # 与生产注销顺序一致：先关闭 MCP 客户端，再 unmark 逐出 provider
        await client.disconnect(timeout=5)
        dt.unmark_dynamic_token_server("gw-evict")
        assert not dt.is_dynamic_token_server("gw-evict")
        assert not any(p._server_id == "gw-evict" for p in dt._ACTIVE_PROVIDERS)
        assert dt._LATEST_PROVIDERS.get("gw-evict") is None

        # 注销后重新连接：无 actor token、保留静态 Authorization（普通 MCP 语义）
        client_static = StreamableHttpClient(cfg)
        try:
            with _turn("Bearer user-A"):
                assert await client_static.connect(timeout=10) is True
                # 普通 MCP 语义：请求级用户令牌不覆盖静态凭据
                assert await client_static.call_tool("whoami", {}, timeout=10) == "static"
        finally:
            await client_static.disconnect(timeout=5)
        last = _last_tool_call(state)
        assert last["authorization"] == "Bearer static"
        assert last["actor"] is None


# ---------------------------------------------------------------------------
# 11. 并发 SSE 重连：发起者凭据不得被等待者覆盖
# ---------------------------------------------------------------------------


async def _scenario_sse_concurrent_reconnect():
    """C 发起重连、D 随后加入等待：本次重连必须使用 C 的令牌。

    回归：捕获值原先写在 SseClient 共享实例属性上，D 会覆盖 C；模拟网关
    只允许 C 时，两条重连都会用 D 而被 401。修复后 C 的令牌随本次 _submit
    命令进入 owner task，D 只等待同一 future，不提交新命令。
    """
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    # 让 C 的重连在途足够久，D 能稳定加入等待而不是另起一次重连
    authority.delay = 0.15
    state = GatewayState(
        "gw-sse-concurrent",
        authority,
        "aud-sse-c",
        accepted_users={"Bearer user-C"},
    )
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_sse_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-sse-concurrent",
            url=gateway_url + "/sse",
            transport="sse",
            dynamic_token_url=keycloak_url + "/token/aud-sse-c",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server(
            "gw-sse-concurrent", entry["dynamic_token_url"]
        )
        client = SseClient(cfg)
        try:
            with _turn("Bearer user-C"):
                assert await client.connect(timeout=10) is True

            state.records.clear()

            async def reconnect_as(user: str) -> bool:
                with _turn(user):
                    return await client.reconnect(timeout=10)

            first = asyncio.create_task(reconnect_as("Bearer user-C"))
            # C 已进入 reconnect（future 已建立并提交命令）后再让 D 加入等待
            for _ in range(200):
                if getattr(client, "_reconnect_future", None) is not None:
                    break
                await asyncio.sleep(0)
            second = asyncio.create_task(reconnect_as("Bearer user-D"))
            results = await asyncio.gather(first, second)

            # D 等待同一次重连，两条都观察到 C 的成功结果
            assert results == [True, True]
            with _turn("Bearer user-C"):
                assert await client.call_tool("whoami", {}, timeout=10) == "user-C"
        finally:
            await client.disconnect(timeout=5)

        assert state.rejections == []
        get_sse = [r for r in state.records if r["path"] == "/sse"]
        assert get_sse and all(
            r["authorization"] == "Bearer user-C" for r in get_sse
        )
        initialize = [r for r in state.records if r["jsonrpc_method"] == "initialize"]
        assert initialize and all(
            r["authorization"] == "Bearer user-C" for r in initialize
        )


# ---------------------------------------------------------------------------
# 12. 连接级回收：断开后 provider / token HTTP 客户端不再滞留
# ---------------------------------------------------------------------------


async def _scenario_provider_lifecycle():
    """连接 + 两次重连 + 断开：每个旧 provider 随会话关闭即回收。"""
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    authority = TokenAuthority("e2e-agent", "e2e-secret")
    state = GatewayState("gw-lifecycle", authority, "aud-life")
    async with _serve(build_keycloak_app(authority)) as keycloak_url, _serve(
        build_sse_gateway(state)
    ) as gateway_url:
        _enable_enterprise()
        entry = _entry(
            name="gw-lifecycle",
            url=gateway_url + "/sse",
            transport="sse",
            dynamic_token_url=keycloak_url + "/token/aud-life",
        )
        cfg = _cfg(entry)
        assert dt.mark_dynamic_token_server("gw-lifecycle", entry["dynamic_token_url"])

        client = SseClient(cfg)
        providers = []
        try:
            with _turn("Bearer user-A"):
                assert await client.connect(timeout=10) is True
                assert await client.call_tool("whoami", {}, timeout=10) == "user-A"
                providers.append(dt._LATEST_PROVIDERS["gw-lifecycle"])
                for _ in range(2):
                    assert await client.reconnect(timeout=10) is True
                    providers.append(dt._LATEST_PROVIDERS["gw-lifecycle"])
                # 重连时旧会话关闭 → 旧 provider 已回收，只保留当前连接
                live = [
                    p for p in dt._ACTIVE_PROVIDERS if p._server_id == "gw-lifecycle"
                ]
                assert len(live) == 1
                assert len({id(p) for p in providers}) == 3
                for old in providers[:-1]:
                    assert old._client is None  # token HTTP 客户端已关闭
        finally:
            await client.disconnect(timeout=5)

        assert not [
            p for p in dt._ACTIVE_PROVIDERS if p._server_id == "gw-lifecycle"
        ]
        assert dt._LATEST_PROVIDERS.get("gw-lifecycle") is None
        for provider in providers:
            assert provider._client is None


# ---------------------------------------------------------------------------
# 进程隔离运行器
#
# 为什么每个场景单独起子进程：当前环境（Windows + Python 3.12 + 该版本 mcp
# SDK）下，一个 streamable-http 会话在**保持连接期间真实等待**后再关闭，
# 会让同进程内下一个 MCP 会话在建连 initialize 时挂死（不加载本补丁、仅用
# 裸 mcp.ClientSession 也可复现，属 SDK/运行时问题）。E2E 每个场景都会
# 起真实 uvicorn + MCP 服务，互相之间没有共享状态需求，因此用子进程隔离
# 保证套件可稳定整体运行；场景内的断言仍然覆盖完整真实链路。
# ---------------------------------------------------------------------------

_E2E_CHILD_ENV = "JIUWENSWARM_MCP_E2E_CHILD"
_E2E_FILE = str(Path(__file__).resolve())
_E2E_ROOT = str(Path(__file__).resolve().parents[2])

SCENARIOS: dict[str, tuple[Any, bool]] = {
    "full_chain_registration": (_scenario_full_chain, True),
    "multiturn_user_switch": (_scenario_multiturn, True),
    "concurrent_sessions": (_scenario_concurrent, True),
    "actor_token_refresh": (_scenario_expiry, True),
    "token_service_degrade": (_scenario_degrade, True),
    "token_service_recover": (_scenario_recover, True),
    "token_service_hard_failure": (_scenario_hard_failure, True),
    "bad_token_http_500": (partial(_scenario_bad_responses, "http_500"), True),
    "bad_token_html": (partial(_scenario_bad_responses, "html"), True),
    "bad_token_empty_json": (partial(_scenario_bad_responses, "empty_json"), True),
    "bad_token_nested_field": (partial(_scenario_bad_responses, "nested_token"), True),
    "multi_gateway_endpoints": (_scenario_multi_gateway, True),
    "sse_reconnect": (_scenario_sse, True),
    "user_token_injection": (_scenario_injection, True),
    "preflight_dual_auth": (_scenario_preflight, True),
    "enterprise_config_wiring": (_scenario_config_wiring, False),
    "credential_extraction": (_scenario_extract, False),
    "unregister_eviction": (_scenario_unregister, True),
    "sse_concurrent_reconnect": (_scenario_sse_concurrent_reconnect, True),
    "provider_lifecycle": (_scenario_provider_lifecycle, True),
}


@pytest.mark.parametrize("scenario_name", list(SCENARIOS))
def test_enterprise_e2e_scenario(scenario_name: str) -> None:
    scenario, is_async = SCENARIOS[scenario_name]
    if os.environ.get(_E2E_CHILD_ENV) == "1":
        if is_async:
            asyncio.run(scenario())
        else:
            scenario()
        return

    node_id = f"{_E2E_FILE}::test_enterprise_e2e_scenario[{scenario_name}]"
    env = dict(os.environ)
    env[_E2E_CHILD_ENV] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            node_id,
            "-o",
            "addopts=",
            "-q",
            "-W",
            "ignore::SyntaxWarning",
        ],
        cwd=_E2E_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    assert proc.returncode == 0, (
        f"E2E scenario {scenario_name!r} failed (exit={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout[-6000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
