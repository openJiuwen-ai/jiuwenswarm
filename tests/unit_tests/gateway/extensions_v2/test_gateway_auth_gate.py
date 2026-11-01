# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authentication gate: the BUILTIN rule that keeps locked frames out."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.events import Event

from jiuwenswarm.gateway.extensions_v2.builtin.auth_gate import (
    AUTH_STORE_KEY,
    build_auth_gate_handler,
)
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    GatewayRequestInput,
    GatewayRequestOutput,
)
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime


class GateChannel:
    """Stand-in for WebChannel that records responses."""

    channel_id = "web"

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self._ids: dict[int, str] = {}

    async def ensure_ws_id(self, ws) -> str:
        key = id(ws)
        if key not in self._ids:
            self._ids[key] = f"ws-{len(self._ids) + 1}"
            setattr(ws, "_jiuwen_ws_id", self._ids[key])
        return self._ids[key]

    def connection_user_id(self, ws):
        return getattr(ws, "_user", None)

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code})


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=GateChannel(), config=GatewayExtensionConfig(install_dir=tmp_path))
    await runtime.start()
    return runtime


def _package(handler, event_type: str = EVENT_GATEWAY_REQUEST, *, auth_provider: bool = True) -> LoadedPackage:
    return LoadedPackage(
        package_id="auth",
        version="1.0.0",
        root=Path("/tmp/auth"),
        handlers=(HandlerDeclaration(event_type, handler, {}, "local-1"),),
        auth_provider=auth_provider,
    )


async def _delegate(event, ops, next_handler):
    return await next_handler(event)


def _authenticate(runtime: GatewayRuntime, connection_id: str = "ws-1") -> None:
    runtime.store.set(
        scope="connection",
        scope_id=connection_id,
        key=AUTH_STORE_KEY,
        value={"token": "tk_1"},
    )


async def test_locked_connection_is_denied_before_native_dispatch(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, params = await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {"a": 1})

    assert visited is True
    assert params == {"a": 1}
    assert runtime.channel.responses == [
        {"id": "r1", "ok": False, "payload": {}, "error": "login required", "code": "AUTH_REQUIRED"}
    ]


async def test_gate_stays_inert_without_an_auth_provider(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate, auth_provider=False))

    visited, params = await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {"a": 1})

    assert runtime.has_auth_provider is False
    assert visited is False
    assert params == {"a": 1}
    assert runtime.channel.responses == []


async def test_publishing_an_auth_provider_turns_the_gate_on(tmp_path):
    runtime = await _runtime(tmp_path)
    assert runtime.has_auth_provider is False

    runtime.publish(_package(_delegate))

    assert runtime.has_auth_provider is True

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert runtime.channel.responses[0]["code"] == "AUTH_REQUIRED"


async def test_authenticated_connection_reaches_core(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))
    _authenticate(runtime)

    visited, params = await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {"a": 1})

    assert visited is False
    assert params == {"a": 1}
    assert runtime.channel.responses == []


async def test_login_capable_method_stays_open_while_locked(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(SimpleNamespace(closed=False), "r1", "webui.dispatch", {})

    assert visited is False
    assert runtime.channel.responses == []


async def test_empty_method_is_left_to_core(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(SimpleNamespace(closed=False), "r1", "   ", {})

    assert visited is True
    assert runtime.channel.responses[0]["code"] == "BAD_REQUEST"


async def test_host_frame_without_a_connection_is_not_gated(tmp_path):
    runtime = await _runtime(tmp_path)

    handler = build_auth_gate_handler(runtime.store)
    from openjiuwen.core.events import Event

    result = await handler(
        Event(EVENT_GATEWAY_REQUEST, __import__(
            "jiuwenswarm.gateway.extensions_v2.models", fromlist=["GatewayRequestInput"]
        ).GatewayRequestInput(method="chat.send", connection_id="")),
        None,
        _next_returning(GatewayRequestOutput(route="forward")),
    )

    assert result.route == "forward"


async def test_locked_callback_runs_once_per_denied_frame(tmp_path):
    runtime = await _runtime(tmp_path)
    calls: list[str] = []

    async def on_locked(connection_id: str) -> None:
        calls.append(connection_id)

    handler = build_auth_gate_handler(runtime.store, on_locked=on_locked)
    from openjiuwen.core.events import Event
    from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestInput

    for _ in range(2):
        result = await handler(
            Event(EVENT_GATEWAY_REQUEST, GatewayRequestInput(method="chat.send", connection_id="ws-9")),
            None,
            _next_returning(GatewayRequestOutput(route="forward")),
        )
        assert result.handled is True

    assert calls == ["ws-9", "ws-9"]


async def test_authenticated_status_is_answered_locally(tmp_path):
    runtime = await _runtime(tmp_path)

    async def local_handler(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="local")

    runtime.publish(_package(local_handler))
    _authenticate(runtime)

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "auth.status", {})

    assert runtime.channel.responses[0]["payload"] == {"authenticated": True}


def _next_returning(value):
    async def event_next(event):
        return value

    return event_next
