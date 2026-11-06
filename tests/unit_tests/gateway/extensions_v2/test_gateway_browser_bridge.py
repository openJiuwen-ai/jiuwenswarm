# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise browser routing with generic packages, without a login example."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiActionOutput, UiRenderInput

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
