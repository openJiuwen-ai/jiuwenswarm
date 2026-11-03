# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authentication gate: the BUILTIN rule that keeps locked frames out.

The gate only requires an ``authorization`` header; token validity belongs to a
USER handler, so every case here drives the decision through ``headers``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.events import Event

from jiuwenswarm.gateway.extensions_v2.builtin.auth_gate import (
    build_auth_gate_handler,
    has_authorization,
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

AUTH_HEADERS = {"authorization": "Bearer tk_1"}


class GateChannel:
    """Stand-in for WebChannel that records responses and downstream frames."""

    channel_id = "web"

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self._sockets: dict[str, SimpleNamespace] = {}
        self._ids: dict[int, str] = {}

    async def ensure_ws_id(self, ws) -> str:
        key = id(ws)
        if key not in self._ids:
            ws_id = f"ws-{len(self._ids) + 1}"
            self._ids[key] = ws_id
            self._sockets[ws_id] = ws
            setattr(ws, "_jiuwen_ws_id", ws_id)
        return self._ids[key]

    def ws_by_id(self, ws_id: str):
        return self._sockets.get(ws_id)

    def connection_user_id(self, ws):
        return getattr(ws, "_user", None)

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code})

    async def send_event(self, ws, event, payload):
        self.events.append((event, payload))


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


async def test_locked_connection_is_denied_before_native_dispatch(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, params = await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {"a": 1})

    assert visited is True
    assert params == {"a": 1}
    assert runtime.channel.responses == [
        {"id": "r1", "ok": False, "payload": {}, "error": "login required", "code": "AUTH_REQUIRED"}
    ]


async def test_locked_frame_never_renders_a_platform_view(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))
    ws = SimpleNamespace(closed=False)

    await runtime.intercept(ws, "r1", "chat.send", {})
    await runtime.intercept(ws, "r2", "chat.send", {})

    assert [response["code"] for response in runtime.channel.responses] == ["AUTH_REQUIRED"] * 2
    assert runtime.channel.events == []


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


async def test_authorization_header_passes_the_frame_to_core(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, params = await runtime.intercept(
        SimpleNamespace(closed=False), "r1", "chat.send", {"a": 1}, AUTH_HEADERS
    )

    assert visited is False
    assert params == {"a": 1}
    assert runtime.channel.responses == []


async def test_blank_or_missing_header_stays_locked(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    for headers in ({}, {"authorization": "   "}, {"X-Token": "tk_1"}):
        visited, _ = await runtime.intercept(
            SimpleNamespace(closed=False), "r1", "chat.send", {}, headers
        )
        assert visited is True

    assert [response["code"] for response in runtime.channel.responses] == ["AUTH_REQUIRED"] * 3


async def test_every_frame_is_checked_on_its_own(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))
    ws = SimpleNamespace(closed=False)

    await runtime.intercept(ws, "r1", "chat.send", {})
    await runtime.intercept(ws, "r2", "chat.send", {}, AUTH_HEADERS)
    await runtime.intercept(ws, "r3", "chat.send", {})

    assert [response["code"] for response in runtime.channel.responses] == ["AUTH_REQUIRED", "AUTH_REQUIRED"]
    assert runtime.channel.events == []


async def test_ping_stays_open_while_locked(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(SimpleNamespace(closed=False), "r1", "ping", {})

    assert visited is False
    assert runtime.channel.responses == []
    assert runtime.channel.events == []


async def test_application_slot_ready_is_accepted_locally(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(
        SimpleNamespace(closed=False),
        "r1",
        "webui.dispatch",
        {"event": "ui.ready", "target": {"slot_id": "application.entry"}},
    )

    assert visited is True
    assert runtime.channel.responses[0] == {
        "id": "r1",
        "ok": True,
        "payload": {"event": "ui.ready", "accepted": True},
        "error": None,
        "code": None,
    }


async def test_application_slot_change_is_consumed_without_a_render(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(
        SimpleNamespace(closed=False),
        "r1",
        "webui.dispatch",
        {"event": "ui.change", "target": {"slot_id": "application.entry"}, "payload": {"value": "u"}},
    )

    assert visited is True
    assert runtime.channel.responses[0]["ok"] is True
    assert runtime.channel.responses[0]["payload"] == {}
    assert runtime.channel.events == []


async def test_application_slot_click_is_consumed_when_no_package_handles_it(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(
        SimpleNamespace(closed=False),
        "r1",
        "webui.dispatch",
        {"event": "ui.click", "target": {"slot_id": "application.entry"}, "payload": {"action_id": "auth.login"}},
    )

    assert visited is True
    assert runtime.channel.responses[0]["ok"] is True
    assert runtime.channel.responses[0]["payload"] == {}


async def test_dispatch_outside_an_application_slot_is_denied(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(
        SimpleNamespace(closed=False),
        "r1",
        "webui.dispatch",
        {"event": "ui.click", "target": {"slot_id": "conversation.panel.right"}},
    )

    assert visited is True
    assert runtime.channel.responses[0]["code"] == "AUTH_REQUIRED"


async def test_empty_method_is_left_to_core(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.publish(_package(_delegate))

    visited, _ = await runtime.intercept(SimpleNamespace(closed=False), "r1", "   ", {})

    assert visited is True
    assert runtime.channel.responses[0]["code"] == "BAD_REQUEST"


async def test_host_frame_without_a_connection_is_not_gated(tmp_path):
    runtime = await _runtime(tmp_path)

    handler = build_auth_gate_handler()

    result = await handler(
        Event(EVENT_GATEWAY_REQUEST, GatewayRequestInput(method="chat.send", connection_id="")),
        None,
        _next_returning(GatewayRequestOutput(route="forward")),
    )

    assert result.route == "forward"


async def test_authenticated_status_is_answered_locally(tmp_path):
    runtime = await _runtime(tmp_path)

    async def local_handler(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="local")

    runtime.publish(_package(local_handler))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "auth.status", {}, AUTH_HEADERS)

    assert runtime.channel.responses[0]["payload"] == {"authenticated": True}


def test_has_authorization_is_case_insensitive_and_requires_a_value():
    assert has_authorization({"Authorization": "Bearer tk"}) is True
    assert has_authorization({"authorization": ""}) is False
    assert has_authorization(None) is False
    assert has_authorization({"other": "x"}) is False


def _next_returning(value):
    async def event_next(event):
        return value

    return event_next
