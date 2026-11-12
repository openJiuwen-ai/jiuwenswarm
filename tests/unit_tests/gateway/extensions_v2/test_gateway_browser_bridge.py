# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise browser routing with generic packages, without a login example."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.management.notifications import notify_installed
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

PACKAGE = "test.browser-controls"


def _channel():
    responses = []
    events = []
    sockets = {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = "browser-one"
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        events.append({"event": event, "payload": payload})

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({"ok": ok, "payload": payload, "error": error, "code": code})

    return SimpleNamespace(
        channel_id="web", responses=responses, events=events, ensure_ws_id=ensure_ws_id,
        connection_user_id=lambda ws: None, ws_by_id=sockets.get,
        send_event=send_event, send_response=send_response,
    )


async def _runtime(tmp_path: Path, policy=None, action=None):
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(install_dir=tmp_path))
    await runtime.start()

    async def ready(event, ops, next_handler):
        return UiActionOutput(accepted=True, status="ready")

    handlers = [HandlerDeclaration("ui.ready", ready, {}, "ready")]
    if policy is not None:
        handlers.append(HandlerDeclaration("gateway.request", policy, {}, "policy"))
    if action is not None:
        handlers.append(HandlerDeclaration("ui.click", action, {"target.package_id": PACKAGE}, "action"))
    runtime.publish(LoadedPackage(
        package_id=PACKAGE, version="1.0.0", root=tmp_path, handlers=tuple(handlers),
        client={"requestBindings": []},
        ui={"apiVersion": 1, "items": [
            {"type": "web.slot.contribution", "slot": "application.entry"},
            {"type": "web.slot.contribution", "slot": "application.sidebar.footer"},
        ]},
    ))
    return runtime


async def _click_params(runtime, ws, slot, *, props=None):
    connection_id = await runtime.connection_id(ws)
    target = RenderTarget(
        session_id="", slot_id=slot, package_id=PACKAGE, instance_key="controls",
        scope="application", connection_id=connection_id,
    )
    button_props = {"value": "Apply"}
    if props is not None:
        button_props.update(props)
    await runtime.bus.trigger(Event("ui.render", UiRenderInput(
        target=target,
        root=RenderNode(component="Button", props=button_props, events={"ui.click": {"id": "apply"}}),
    )), SessionContext("gateway:" + connection_id, None, {"connection_id": connection_id}))
    record = runtime.registry.get(target)
    assert record is not None and record.root is not None
    return {"event": "ui.click", "target": target.to_payload(), "payload": {
        "node_key": "0", "revision": record.revision, "action_id": "apply", "args": {}, "data": {},
    }}


@pytest.mark.parametrize("method", ["local_storage.set", "local_storage.delete", "page.reload"])
async def test_active_client_package_can_use_browser_operations_without_grants(tmp_path, method):
    async def action(event, ops, next_handler):
        if method == "local_storage.set":
            await ops.client.local_storage_set("record", {"value": "example"})
        elif method == "local_storage.delete":
            await ops.client.local_storage_delete("record")
        else:
            await ops.client.page_reload()
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, action=action)
    try:
        ws = SimpleNamespace(closed=False)
        params = await _click_params(runtime, ws, "application.entry")
        await runtime.intercept(ws, "action", "webui.dispatch", params)
        assert runtime.channel.responses[-1]["payload"] == {"accepted": True, "status": "applied"}
        operations = [item["payload"] for item in runtime.channel.events if item["event"] == "client.operation"]
        assert len(operations) == 1
        assert operations[0]["method"] == method
        assert operations[0]["package_id"] == PACKAGE
        assert operations[0]["activation_generation"] == runtime.browser.generations[PACKAGE]
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("props,data", [
    ({"disabled": True}, {}), ({}, []), ({}, None), ({}, {"extra": "value"}),
])
async def test_invalid_button_actions_do_not_reach_the_handler(tmp_path, props, data):
    actions = []

    async def action(event, ops, next_handler):
        actions.append(event.event_input)
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, action=action)
    try:
        ws = SimpleNamespace(closed=False)
        params = await _click_params(runtime, ws, "application.entry", props=props)
        params["payload"]["data"] = data
        await runtime.intercept(ws, "action", "webui.dispatch", params)
        assert actions == []
        assert runtime.channel.responses[-1]["payload"]["status"] == "invalid_action"
    finally:
        await runtime.shutdown()


async def test_request_policy_denies_before_application_action(tmp_path):
    observed = []
    actions = []

    async def deny(event, ops, next_handler):
        observed.append(event.event_input.method)
        return GatewayRequestOutput(handled=True, route="deny", error={"code": "POLICY_DENIED"})

    async def action(event, ops, next_handler):
        actions.append(event.event_input)
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, deny, action)
    try:
        ws = SimpleNamespace(closed=False)
        params = await _click_params(runtime, ws, "application.sidebar.footer")
        visited, _ = await runtime.intercept(ws, "action", "webui.dispatch", params)
        assert visited is True
        assert observed == ["webui.dispatch"]
        assert actions == []
        assert runtime.channel.responses[-1]["code"] == "POLICY_DENIED"
    finally:
        await runtime.shutdown()


async def test_plugin_can_allow_anonymous_entry_and_deny_footer(tmp_path):
    observed = []
    actions = []

    async def policy(event, ops, next_handler):
        request = event.event_input
        slot = request.params["target"]["slot_id"]
        observed.append(slot)
        if slot == "application.entry":
            return await next_handler(event)
        return GatewayRequestOutput(handled=True, route="deny", error={"code": "POLICY_DENIED"})

    async def action(event, ops, next_handler):
        actions.append(event.event_input)
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, policy, action)
    try:
        ws = SimpleNamespace(closed=False)
        entry = await _click_params(runtime, ws, "application.entry")
        footer = await _click_params(runtime, ws, "application.sidebar.footer")
        await runtime.intercept(ws, "entry", "webui.dispatch", entry, request_context={"tenant": "one"})
        assert runtime.channel.responses[-1]["payload"] == {"accepted": True, "status": "applied"}
        assert len(actions) == 1
        assert actions[0].request_context == {"tenant": "one"}
        await runtime.intercept(ws, "footer", "webui.dispatch", footer)
        assert runtime.channel.responses[-1]["code"] == "POLICY_DENIED"
        assert observed == ["application.entry", "application.sidebar.footer"]
        assert len(actions) == 1
    finally:
        await runtime.shutdown()


async def test_request_policy_can_consume_without_dispatching_ui(tmp_path):
    actions = []

    async def policy(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="consume", response={"source": "policy"})

    async def action(event, ops, next_handler):
        actions.append(event.event_input)
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, policy, action)
    try:
        ws = SimpleNamespace(closed=False)
        params = await _click_params(runtime, ws, "application.entry")
        await runtime.intercept(ws, "action", "webui.dispatch", params)
        assert runtime.channel.responses[-1]["payload"] == {"source": "policy"}
        assert actions == []
    finally:
        await runtime.shutdown()


async def test_other_package_ui_keeps_the_existing_forward_path(tmp_path):
    runtime = await _runtime(tmp_path)
    try:
        params = {"event": "ui.ready", "target": {
            "scope": "application", "slot_id": "application.sidebar.footer", "package_id": "other.package",
        }}
        visited, returned = await runtime.intercept(SimpleNamespace(closed=False), "other", "webui.dispatch", params)
        assert visited is False
        assert returned == params
        assert runtime.channel.responses == []
    finally:
        await runtime.shutdown()


async def test_shared_footer_ready_initializes_gateway_and_keeps_agentserver_forward_path(tmp_path):
    observed = []
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(install_dir=tmp_path))
    await runtime.start()

    async def ready(event, ops, next_handler):
        del ops, next_handler
        observed.append(event.event_input.target.package_id)
        return UiActionOutput(accepted=True, status="ready")

    runtime.publish(LoadedPackage(
        package_id=PACKAGE,
        version="1.0.0",
        root=tmp_path,
        handlers=(HandlerDeclaration("ui.ready", ready, {}, "ready"),),
        client={},
        ui={"apiVersion": 1, "items": [
            {"type": "web.slot.contribution", "slot": "application.sidebar.footer"},
        ]},
    ))
    try:
        params = {
            "event": "ui.ready",
            "target": {"scope": "application", "slot_id": "application.sidebar.footer", "package_id": ""},
            "payload": {},
        }
        visited, returned = await runtime.intercept(SimpleNamespace(closed=False), "shared", "webui.dispatch", params)
        assert visited is False
        assert returned == params
        assert observed == [PACKAGE]
        assert runtime.channel.responses == []
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("mode", ["no-policy", "continue", "forward"])
async def test_http_default_forward_keeps_the_native_route(tmp_path, mode):
    async def policy(event, ops, next_handler):
        if mode == "continue":
            return await next_handler(event)
        return GatewayRequestOutput(route="forward")

    runtime = await _runtime(tmp_path, None if mode == "no-policy" else policy)
    try:
        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)

        @app.get("/business")
        async def business():
            return {"source": "native"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/business")
        assert response.status_code == 200
        assert response.json() == {"source": "native"}
        assert "access-control-allow-origin" not in response.headers
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("route", ["deny", "consume"])
@pytest.mark.parametrize("path", ["/business", "/extension-assets/test.browser-controls/1/page.js"])
async def test_http_keeps_plugin_denial_and_consumption(tmp_path, route, path):
    async def policy(event, ops, next_handler):
        return GatewayRequestOutput(
            handled=True, route=route, response={"source": "policy"},
            error={"code": "POLICY_DENIED", "status": 403},
        )

    runtime = await _runtime(tmp_path, policy)
    try:
        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)
        native_calls = []

        @app.get("/business")
        async def business():
            native_calls.append(True)
            return {}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(path, headers={"Origin": "null"})
        assert response.status_code == (403 if route == "deny" else 200)
        assert response.json() == ({"code": "POLICY_DENIED"} if route == "deny" else {"source": "policy"})
        assert native_calls == []
        assert "access-control-allow-origin" not in response.headers
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("source", ["active", "missing-runtime", "missing-asset", "inactive-package"])
async def test_package_asset_cors_is_scoped_to_file_responses(tmp_path, method, source):
    asset = tmp_path / "module.js"
    asset.write_text("export const value = 1;", encoding="utf-8")

    def asset_file(package_id, generation, path):
        assert (package_id, generation, path) == (PACKAGE, "1", "module.js")
        if source == "missing-asset":
            raise ValueError("missing asset")
        if source == "inactive-package":
            raise StopIteration
        return asset

    runtime = SimpleNamespace(browser=SimpleNamespace(enabled=False, asset_file=asset_file))
    app = FastAPI()
    attach_browser_routes(app, lambda: None if source == "missing-runtime" else runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, f"/extension-assets/{PACKAGE}/1/module.js", headers={"Origin": "null"})
    assert response.status_code == (200 if source == "active" else 404)
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    if method == "HEAD":
        assert response.content == b""
    if source == "active":
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


async def test_frames_a_render_causes_carry_the_activation_that_rendered(tmp_path):
    """Render, error and clear frames name the calling activation; the transport takes it from the CORE handler."""
    sent: dict = {}

    async def action(event, ops, next_handler):
        target = event.event_input.target
        sent["render"] = await ops.ui.render(UiRenderInput(target=target, root=RenderNode(component="Button",
                                                                                         props={"value": "Done"})))
        sent["error"] = await ops.ui.render(UiRenderInput(target=target, root=RenderNode(component="NoSuchComponent")))
        sent["clear"] = await ops.ui.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _runtime(tmp_path, action=action)
    try:
        ws = SimpleNamespace(closed=False)
        params = await _click_params(runtime, ws, "application.entry")
        runtime.channel.events.clear()
        await runtime.intercept(ws, "action", "webui.dispatch", params)
        generation = runtime.browser.generations[PACKAGE]
        names = {"webui.render", "webui.render.error", "webui.render.clear"}
        frames = {item["event"]: item["payload"] for item in runtime.channel.events if item["event"] in names}
        assert set(frames) == names, runtime.channel.events
        assert {name: frame.get("activation_generation") for name, frame in frames.items()} == dict.fromkeys(names, generation)
    finally:
        await runtime.shutdown()


async def _ready(runtime, ws, slot):
    await runtime.intercept(ws, "ready", "webui.dispatch", {
        "event": "ui.ready",
        "target": {"scope": "application", "slot_id": slot, "package_id": PACKAGE, "instance_key": "default"},
        "payload": {},
    })


async def test_release_clears_the_retired_generation_before_withdrawing_declarations(tmp_path):
    """A disable or uninstall removes the mounted view without a page reload (S_06)."""
    runtime = await _runtime(tmp_path)
    try:
        ws = SimpleNamespace(closed=False)
        await _ready(runtime, ws, "application.sidebar.footer")
        await _click_params(runtime, ws, "application.sidebar.footer")
        generation = runtime.browser.generations[PACKAGE]
        runtime.channel.events.clear()

        runtime.unpublish(PACKAGE)
        await runtime.browser.settle()

        events = runtime.channel.events
        clears = [item for item in events if item["event"] == "webui.render.clear"]
        declarations = [item for item in events if item["event"] == "client.declarations"]
        assert len(clears) == 1, events
        assert clears[0]["payload"]["target"]["slot_id"] == "application.sidebar.footer"
        assert clears[0]["payload"]["activation_generation"] == generation
        assert declarations and declarations[-1]["payload"]["packages"] == []
        assert events.index(clears[0]) < events.index(declarations[-1])
    finally:
        await runtime.shutdown()


async def test_a_new_connection_gets_declarations_while_a_release_is_pending(tmp_path):
    """A release under way must not withhold declarations from another browser (S_06)."""
    runtime = await _runtime(tmp_path)
    release_cleared, release_may_finish = asyncio.Event(), asyncio.Event()
    try:
        ws = SimpleNamespace(closed=False)
        await _ready(runtime, ws, "application.sidebar.footer")
        await _click_params(runtime, ws, "application.sidebar.footer")
        original_send = runtime.channel.send_event

        async def send_event(target, event, payload):
            if event == "webui.render.clear":
                release_cleared.set()
                await release_may_finish.wait()
            await original_send(target, event, payload)

        runtime.channel.send_event = send_event
        runtime.channel.events.clear()
        runtime.unpublish(PACKAGE)
        await release_cleared.wait()

        await runtime.browser.connect(SimpleNamespace(closed=False), "browser-2")

        declarations = [item for item in runtime.channel.events if item["event"] == "client.declarations"]
        assert declarations, runtime.channel.events
        assert declarations[-1]["payload"]["packages"] == []
    finally:
        release_may_finish.set()
        await runtime.browser.settle()
        await runtime.shutdown()


async def test_declarations_wait_until_the_retired_view_is_cleared(tmp_path):
    """A re-activated package must not be declared before its clear frame arrives (S_06)."""
    runtime = await _runtime(tmp_path)
    try:
        ws = SimpleNamespace(closed=False)
        await _ready(runtime, ws, "application.sidebar.footer")
        await _click_params(runtime, ws, "application.sidebar.footer")
        runtime.channel.events.clear()

        # Replacing the running version releases the old activation first: its clear frame is
        # still outstanding when the new generation becomes the active one.
        runtime.publish(runtime.active_packages()[0])
        await runtime.browser.connect(ws, "browser-one")

        # Declaring the new generation now would make the retired clear unacceptable and
        # leave the old view mounted, so the connection has to wait for the release.
        assert [item for item in runtime.channel.events if item["event"] == "client.declarations"] == []

        await runtime.browser.settle()

        declarations = [item for item in runtime.channel.events if item["event"] == "client.declarations"]
        clears = [item for item in runtime.channel.events if item["event"] == "webui.render.clear"]
        assert declarations and clears, runtime.channel.events
        assert runtime.channel.events.index(clears[0]) < runtime.channel.events.index(declarations[-1])
        assert declarations[-1]["payload"]["packages"][0]["activationGeneration"] == (
            runtime.browser.generations[PACKAGE]
        )
    finally:
        await runtime.browser.settle()
        await runtime.shutdown()


async def test_notify_installed_publishes_declarations_before_asking_slots_to_render(tmp_path):
    """Re-enabling a package must refresh declarations before the slot renders again (S_06)."""
    runtime = await _runtime(tmp_path)
    try:
        ws = SimpleNamespace(closed=False)
        await _ready(runtime, ws, "application.sidebar.footer")
        package = runtime.active_packages()[0]
        runtime.unpublish(PACKAGE)
        await runtime.browser.settle()
        runtime.publish(package)
        notices = []
        original_send = runtime.channel.send_event

        async def send_event(ws, event, payload):
            notices.append((event, payload))
            await original_send(ws, event, payload)

        async def broadcast(event, payload):
            notices.append((event, payload))

        runtime.channel.send_event = send_event
        runtime.channel.broadcast_event = broadcast
        runtime.channel.events.clear()

        assert await notify_installed(runtime, [PACKAGE]) is True

        # Declarations must reach the browser before it is asked to render the package again.
        assert [event for event, _ in notices] == ["client.declarations", "extensions.v2.installed"]
        assert notices[1][1] == {"package_ids": [PACKAGE]}
        assert notices[0][1]["packages"][0]["activationGeneration"] == runtime.browser.generations[PACKAGE]
    finally:
        await runtime.shutdown()

