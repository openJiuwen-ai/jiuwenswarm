# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for WebChannel same-port HTTP+WS (FastAPI/uvicorn).

Avoid fastapi/starlette TestClient: some CI images ship a Starlette that requires
``httpx2`` and fail at import time. Exercise FastAPI route registration and the
public ``handle_connection`` path with a lightweight fake websocket instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import (
    _reject_disallowed_origin,
    _serve_channel_websocket,
    build_web_channel_app,
    normalize_web_ws_path,
)
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.channel_manager.web.ws_connection_adapter import StarletteWsAdapter


def _make_channel(**kwargs: Any) -> WebChannel:
    cfg = WebChannelConfig(enabled=True, **kwargs)
    return WebChannel(cfg, RobotMessageRouter())


def _route_paths(app: Any) -> set[str | None]:
    return {getattr(r, "path", None) for r in app.router.routes}


class _QueueWebSocket:
    """Minimal websockets-like surface for ``WebChannel.handle_connection``."""

    def __init__(self, path: str, inbound: list[str]) -> None:
        self.path = path
        self.remote_address = ("127.0.0.1", 12345)
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.sent: list[dict[str, Any]] = []
        self._inbound = list(inbound)
        self._idx = 0

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    def __aiter__(self) -> _QueueWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._idx >= len(self._inbound):
            raise StopAsyncIteration
        item = self._inbound[self._idx]
        self._idx += 1
        return item


def test_build_app_exposes_ws_routes_only() -> None:
    channel = _make_channel()
    app = build_web_channel_app(channel)
    paths = _route_paths(app)
    assert "/ws" in paths
    assert "/ws/git" in paths
    assert "/ws/plugin/test" not in paths
    assert "/container-file-api/upload" not in paths
    assert "/file-api/upload" not in paths


def test_build_app_mounts_application_plugin_websocket_routes() -> None:
    from types import SimpleNamespace

    from jiuwenswarm.extensions.sdk import WebSocketRouteContribution

    async def endpoint(websocket) -> None:  # noqa: ANN001
        await websocket.close()

    plugin = SimpleNamespace(
        plugin_id="test-plugin",
        metadata=SimpleNamespace(id="test-plugin"),
        is_enabled=lambda: True,
        websocket_routes=lambda: (
            WebSocketRouteContribution(path="/ws/plugin/test", endpoint=endpoint),
        ),
    )
    channel = _make_channel()
    channel.application_plugin_registry = SimpleNamespace(
        get_application_plugins=lambda: (plugin,),
        get_application_plugin=lambda plugin_id: plugin if plugin_id == "test-plugin" else None,
    )

    paths = _route_paths(build_web_channel_app(channel))

    assert "/ws/plugin/test" in paths


def test_build_app_registers_custom_config_path() -> None:
    channel = _make_channel(path="/custom")
    app = build_web_channel_app(channel)
    paths = _route_paths(app)
    assert "/custom" in paths
    assert "/ws/git" in paths
    assert "/ws" not in paths


def test_normalize_web_ws_path() -> None:
    assert normalize_web_ws_path(None) == "/ws"
    assert normalize_web_ws_path("") == "/ws"
    assert normalize_web_ws_path("custom") == "/custom"
    assert normalize_web_ws_path("/custom/") == "/custom"


def _register_ping(channel: WebChannel) -> None:
    channel.on_message(lambda _msg: False)

    async def _ping(ws, req_id, params, session_id):  # noqa: ANN001
        await channel.send_response(
            ws, req_id, ok=True, payload={"pong": True, "session_id": session_id},
        )

    channel.register_method("test.ping", _ping)


async def _ping_roundtrip(channel: WebChannel, path: str) -> None:
    _register_ping(channel)
    req = json.dumps(
        {
            "type": "req",
            "id": "r1",
            "method": "test.ping",
            "params": {},
        }
    )
    ws = _QueueWebSocket(f"{path}?user_id=alice&session_id=sess-1", [req])
    await channel.handle_connection(ws, path=ws.path)
    # Writer may flush asynchronously after handler returns; give it a tick.
    await asyncio.sleep(0)
    for _ in range(20):
        if any(f.get("type") == "res" and f.get("id") == "r1" for f in ws.sent):
            break
        await asyncio.sleep(0.01)
    matches = [f for f in ws.sent if f.get("type") == "res" and f.get("id") == "r1"]
    assert matches, f"no ping response in {ws.sent!r}"
    assert matches[0]["ok"] is True
    assert matches[0]["payload"]["pong"] is True


@pytest.mark.asyncio
async def test_dual_protocol_ws_jsonrpc_roundtrip() -> None:
    await _ping_roundtrip(_make_channel(), "/ws")


@pytest.mark.asyncio
async def test_dual_protocol_custom_path_jsonrpc_roundtrip() -> None:
    await _ping_roundtrip(_make_channel(path="/custom"), "/custom")


@pytest.mark.asyncio
async def test_dual_protocol_ws_git_path_closes_without_registry() -> None:
    """/ws/git accepts then closes with 1011 if git registry is absent."""
    channel = _make_channel()
    ws = _QueueWebSocket("/ws/git?user_id=alice", [])
    await channel.handle_connection(ws, path=ws.path)
    assert ws.closed is True
    assert ws.close_code == 1011


@pytest.mark.asyncio
async def test_git_websocket_query_rejects_oversized_session_id_before_registration() -> None:
    """/ws/git must reject an oversized query session_id like /ws does."""
    channel = _make_channel()
    oversized = "s" * 10_240
    ws = _QueueWebSocket(f"/ws/git?user_id=alice&session_id={oversized}", [])

    await channel.handle_connection(ws, path=ws.path)

    assert ws.closed is True
    assert ws.close_code == 1008
    assert ws.close_reason == "session_id exceeds maximum length 80"
    assert channel.clients == set()

    # Rejection is connection-local; the channel remains responsive.
    await _ping_roundtrip(channel, "/ws")


@pytest.mark.asyncio
async def test_websocket_query_rejects_oversized_session_id_before_registration() -> None:
    channel = _make_channel()
    oversized = "s" * 10_240
    ws = _QueueWebSocket(f"/ws?user_id=alice&session_id={oversized}", [])

    await channel.handle_connection(ws, path=ws.path)

    assert ws.closed is True
    assert ws.close_code == 1008
    assert ws.close_reason == "session_id exceeds maximum length 80"
    assert channel.clients == set()

    # Rejection is connection-local; the channel remains responsive.
    await _ping_roundtrip(channel, "/ws")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected_error"),
    [
        (
            {"session_id": "s" * 10_240, "run_id": "run-1"},
            "session_id exceeds maximum length 80",
        ),
        (
            {"session_id": "sess-1", "run_id": "r" * 10_240},
            "run_id exceeds maximum length 256",
        ),
    ],
)
async def test_swarmflow_control_rejects_oversized_ids_before_forwarding(
    params: dict[str, str], expected_error: str,
) -> None:
    channel = _make_channel()
    forwarded: list[Any] = []
    channel.on_message(lambda message: forwarded.append(message))
    req = json.dumps(
        {
            "type": "req",
            "id": "oversized-control",
            "method": "swarmflow.pause",
            "params": params,
        }
    )
    ws = _QueueWebSocket("/ws?user_id=alice&session_id=sess-1", [req])

    await channel.handle_connection(ws, path=ws.path)
    await asyncio.sleep(0)

    responses = [
        frame for frame in ws.sent
        if frame.get("type") == "res" and frame.get("id") == "oversized-control"
    ]
    assert responses == [
        {
            "type": "res",
            "id": "oversized-control",
            "ok": False,
            "payload": {},
            "error": expected_error,
            "code": "BAD_REQUEST",
        }
    ]
    assert forwarded == []


@pytest.mark.asyncio
async def test_origin_rejected_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_ENABLE_ORIGIN_CHECK", "1")
    monkeypatch.setenv("JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS", "allowed.example")

    websocket = MagicMock()
    websocket.headers = {"origin": "https://evil.example"}
    websocket.url.path = "/ws"
    websocket.send_denial_response = AsyncMock()

    rejected = await _reject_disallowed_origin(websocket)
    assert rejected is True
    websocket.send_denial_response.assert_awaited_once()


def test_starlette_adapter_path_includes_query() -> None:
    websocket = MagicMock()
    websocket.url.path = "/ws"
    websocket.url.query = "token=abc&user_id=u1"
    websocket.client = MagicMock(host="127.0.0.1", port=9)
    websocket.scope = {"server": ("0.0.0.0", 19000)}
    websocket.headers = {"x-user-id": "u1"}
    websocket.client_state = MagicMock()

    adapter = StarletteWsAdapter(websocket)
    assert adapter.path == "/ws?token=abc&user_id=u1"
    assert adapter.remote_address == ("127.0.0.1", 9)
    assert adapter.request_headers.get("x-user-id") == "u1"


@pytest.mark.asyncio
async def test_serve_ws_does_not_accept_when_unauthorized() -> None:
    channel = _make_channel()

    async def deny(**_kwargs: Any) -> AuthResult:
        return AuthResult(success=False, error="Token 无效或已过期")

    channel.set_handshake_auth(deny)
    websocket = MagicMock()
    websocket.headers = {}
    websocket.url.path = "/ws"
    websocket.url.query = "token=fake"
    websocket.client = MagicMock(host="127.0.0.1", port=9)
    websocket.send_denial_response = AsyncMock()
    websocket.accept = AsyncMock()
    await _serve_channel_websocket(channel, websocket)
    websocket.accept.assert_not_called()
    websocket.send_denial_response.assert_awaited_once()
    assert websocket.send_denial_response.await_args.args[0].status_code == 401


@pytest.mark.asyncio
async def test_serve_ws_uses_authenticated_identity_over_query() -> None:
    channel = _make_channel()
    calls = []

    async def authenticate(**_kwargs: Any) -> AuthResult:
        calls.append("auth")
        return AuthResult(success=True, user_id="owner")

    async def handle_connection(adapter, *, path):
        calls.append("connect")
        assert path == "/ws?user_id=other"
        assert channel._resolve_connection_user_id({"user_id": "other"}, adapter) == "owner"

    channel.set_handshake_auth(authenticate)
    channel.handle_connection = handle_connection
    websocket = MagicMock()
    websocket.headers = {}
    websocket.url.path = "/ws"
    websocket.url.query = "user_id=other"
    websocket.client = MagicMock(host="127.0.0.1", port=9)
    websocket.scope = {}
    websocket.accept = AsyncMock()

    await _serve_channel_websocket(channel, websocket)

    assert calls == ["auth", "connect"]
    websocket.accept.assert_awaited_once()
