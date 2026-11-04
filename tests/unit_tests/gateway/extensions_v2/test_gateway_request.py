# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway runtime bootstrap and the ``gateway.request`` trigger point."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    GatewayRequestInput,
    GatewayRequestOutput,
)
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserConnection
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput


class FakeChannel:
    """Minimal stand-in for WebChannel used by the runtime."""

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
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


def _ws(user: str | None = None) -> SimpleNamespace:
    ws = SimpleNamespace(closed=False)
    if user is not None:
        ws._user = user
    return ws


def _package(handler, event_type: str = EVENT_GATEWAY_REQUEST) -> LoadedPackage:
    return LoadedPackage(
        package_id="demo",
        version="1.0.0",
        root=Path("/tmp/demo"),
        handlers=(HandlerDeclaration(event_type, handler, {}, "local-1"),),
        auth_provider=True,
    )


async def _runtime(tmp_path: Path, channel: FakeChannel | None = None) -> GatewayRuntime:
    runtime = GatewayRuntime(
        channel=channel or FakeChannel(),
        config=GatewayExtensionConfig(install_dir=tmp_path),
    )
    await runtime.start()
    return runtime


#: Header that keeps the login gate open; the gate only checks presence.
AUTH_HEADERS = {"authorization": "Bearer tk_1"}


async def test_no_user_handler_keeps_the_frame_untouched(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)
    ws = _ws()

    visited, params = await runtime.intercept(ws, "r1", "chat.send", {"session_id": "s1"})

    assert visited is False
    assert params == {"session_id": "s1"}
    assert runtime.active is False
    assert not hasattr(ws, "_jiuwen_ws_id")
    assert channel.responses == []


async def test_request_context_preserves_plugin_fields_without_changing_params(tmp_path):
    runtime = await _runtime(tmp_path)
    observed = []

    async def capture(event, ops, next_handler):
        observed.append(event.event_input)
        return await next_handler(event)

    runtime.publish(_package(capture))
    try:
        visited, params = await runtime.intercept(_ws(), "context", "chat.send", {"message": "hello"},
            request_context={"tenant_id": "tenant-a", "headers": {"Authorization": "Bearer example"}})
        assert visited is False
        assert params == {"message": "hello"}
        assert observed[0].request_context["tenant_id"] == "tenant-a"
        assert observed[0].headers == {"authorization": "Bearer example"}
    finally:
        await runtime.shutdown()


async def test_core_defaults_to_forward(tmp_path):
    runtime = await _runtime(tmp_path)

    result = await runtime.bus.trigger(
        Event(EVENT_GATEWAY_REQUEST, GatewayRequestInput(method="ping", connection_id="ws-1")),
        SessionContext("gateway:ws-1"),
    )

    assert isinstance(result, GatewayRequestOutput)
    assert result.handled is False
    assert result.route == "forward"


async def test_core_denies_an_empty_method(tmp_path):
    runtime = await _runtime(tmp_path)

    result = await runtime.bus.trigger(
        Event(EVENT_GATEWAY_REQUEST, GatewayRequestInput(method="  ", connection_id="ws-1")),
        SessionContext("gateway:ws-1"),
    )

    assert result.handled is True
    assert result.route == "deny"
    assert result.error == {"code": "BAD_REQUEST", "message": "method is required"}


async def test_handler_can_rewrite_params_and_continue(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        params = dict(event.event_input.params)
        params["rewritten"] = True
        return GatewayRequestOutput(route="forward", params=params)

    runtime.publish(_package(handler))
    ws = _ws(user="alice")

    visited, params = await runtime.intercept(ws, "r1", "chat.send", {"session_id": "s1"})

    assert visited is False
    assert params == {"session_id": "s1", "rewritten": True}
    assert channel.responses == []


async def test_handler_can_consume_the_frame(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        if event.event_input.method != "config.get":
            return await next_handler(event)
        return GatewayRequestOutput(handled=True, route="consume", response={"source": "gateway"})

    runtime.publish(_package(handler))
    ws = _ws()

    visited, _ = await runtime.intercept(ws, "r9", "config.get", {})

    assert visited is True
    assert channel.responses == [
        {"id": "r9", "ok": True, "payload": {"source": "gateway"}, "error": None, "code": None}
    ]


async def test_handler_can_deny_the_frame(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        return GatewayRequestOutput(
            handled=True,
            route="deny",
            error={"code": "AUTH_REQUIRED", "message": "login first"},
        )

    runtime.publish(_package(handler))

    visited, _ = await runtime.intercept(_ws(), "r2", "chat.send", {})

    assert visited is True
    assert channel.responses[0]["ok"] is False
    assert channel.responses[0]["code"] == "AUTH_REQUIRED"
    assert channel.responses[0]["error"] == "login first"


async def test_local_route_is_answered_by_the_builtin_router(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="local")

    runtime.publish(_package(handler))

    visited, _ = await runtime.intercept(_ws(), "r3", "auth.status", {}, AUTH_HEADERS)

    assert visited is True
    assert channel.responses == [
        {"id": "r3", "ok": True, "payload": {"authenticated": True}, "error": None, "code": None}
    ]


async def test_local_route_rejects_an_unknown_method(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="local")

    runtime.publish(_package(handler))

    visited, _ = await runtime.intercept(_ws(), "r3", "chat.send", {}, AUTH_HEADERS)

    assert visited is True
    assert channel.responses[0]["ok"] is False
    assert channel.responses[0]["code"] == "ROUTE_UNSUPPORTED"
    assert channel.responses[0]["error"] == "no gateway builtin for method: chat.send"


async def test_ops_gateway_request_triggers_a_nested_event(tmp_path):
    runtime = await _runtime(tmp_path)

    async def handler(event, ops, next_handler):
        if event.event_input.method == "inner":
            return await next_handler(event)
        nested = await ops.gateway.request(
            GatewayRequestInput(
                method="inner",
                connection_id=event.event_input.connection_id,
                headers=event.event_input.headers,
            )
        )
        return GatewayRequestOutput(
            handled=True,
            route="consume",
            response={"nested_route": nested.route},
        )

    runtime.publish(_package(handler))
    channel = runtime.channel

    visited, _ = await runtime.intercept(_ws(), "r4", "chat.send", {}, AUTH_HEADERS)

    assert visited is True
    assert channel.responses[0]["payload"] == {"nested_route": "forward"}


async def test_disconnect_drops_connection_state(tmp_path):
    runtime = await _runtime(tmp_path)
    runtime.store.set(scope="connection", scope_id="ws-1", key="token", value="abc")

    await runtime.on_disconnect(SimpleNamespace(_jiuwen_ws_id="ws-1"))

    assert runtime.store.get(scope="connection", scope_id="ws-1", key="token") is None


async def test_registered_disconnect_hook_cleans_only_the_closed_connection_and_allows_reconnect(tmp_path, monkeypatch):
    from jiuwenswarm.gateway.extensions_v2 import assembly

    class DisconnectChannel(FakeChannel):
        def __init__(self):
            super().__init__()
            self.disconnect_hooks = []

        def on_disconnect(self, hook):
            self.disconnect_hooks.append(hook)

    channel = DisconnectChannel()
    runtime = await _runtime(tmp_path, channel)
    disconnected = []

    async def handler(event, ops, next_handler):
        disconnected.append(event.event_input.connection_id)
        return await next_handler(event)

    runtime.publish(_package(handler, "gateway.disconnect"))
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    assembly._hook_disconnect(channel)
    old_ws, other_ws = _ws(), _ws()
    old_id = await channel.ensure_ws_id(old_ws)
    other_id = await channel.ensure_ws_id(other_ws)
    try:
        for connection_id, ws in ((old_id, old_ws), (other_id, other_ws)):
            runtime.browser.connections[connection_id] = BrowserConnection(ws)
            runtime.store.set(scope="connection", scope_id=connection_id, key="draft", value="saved")
            target = RenderTarget(session_id="", slot_id="application.entry", package_id="demo",
                                  instance_key="view", scope="application", connection_id=connection_id)
            async with runtime.registry.claim(target) as claim:
                runtime.registry.commit(claim.instance, UiRenderInput(
                    target=target, root=RenderNode(component="Text", props={"value": "View"}),
                ))
        old_ws.closed = True
        assert len(channel.disconnect_hooks) == 1
        await channel.disconnect_hooks[0](old_ws, {"session-1"})
        assert disconnected == [old_id]
        assert old_id not in runtime.browser.connections
        assert runtime.store.get(scope="connection", scope_id=old_id, key="draft") is None
        assert [record.target.connection_id for record in runtime.registry.snapshot()] == [other_id]
        assert other_id in runtime.browser.connections
        assert runtime.store.get(scope="connection", scope_id=other_id, key="draft").value == "saved"
        new_ws = _ws()
        visited, params = await runtime.intercept(new_ws, "reconnect", "ping", {})
        assert not visited and params == {}
        assert new_ws._jiuwen_ws_id not in {old_id, other_id}
    finally:
        await runtime.shutdown()


async def test_disconnect_entry_accepts_channel_arguments_when_runtime_is_disabled(monkeypatch):
    from jiuwenswarm.gateway.extensions_v2 import assembly, notify_gateway_disconnect

    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: None)
    await notify_gateway_disconnect(_ws(), {"session-1"})


async def test_disconnect_entry_preserves_channel_cleanup_when_extensions_raise(monkeypatch):
    from jiuwenswarm.gateway.extensions_v2 import assembly, notify_gateway_disconnect

    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: SimpleNamespace(on_disconnect=cleanup))
    ws, sessions = _ws(), {"session-1"}
    await notify_gateway_disconnect(ws, sessions)
    cleanup.assert_awaited_once_with(ws, sessions)


async def test_broken_handler_does_not_break_the_frame(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        raise RuntimeError("boom")

    runtime.publish(_package(handler))

    try:
        visited, params = await runtime.intercept(_ws(), "r5", "chat.send", {"a": 1}, AUTH_HEADERS)

        assert visited is False
        assert params == {"a": 1}
        assert channel.responses == []
    finally:
        await runtime.shutdown()


async def test_shutdown_unregisters_every_handler(tmp_path):
    runtime = await _runtime(tmp_path)

    async def handler(event, ops, next_handler):
        return await next_handler(event)

    runtime.publish(_package(handler))
    assert runtime.user_handler_count == 1

    await runtime.shutdown()

    assert runtime.user_handler_count == 0
    assert runtime.active is False


async def test_unknown_route_falls_back_to_forward(tmp_path):
    channel = FakeChannel()
    runtime = await _runtime(tmp_path, channel)

    async def handler(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="nonsense")

    runtime.publish(_package(handler))

    visited, params = await runtime.intercept(_ws(), "r6", "chat.send", {"k": 1})

    assert visited is False
    assert params == {"k": 1}
    assert channel.responses == []
