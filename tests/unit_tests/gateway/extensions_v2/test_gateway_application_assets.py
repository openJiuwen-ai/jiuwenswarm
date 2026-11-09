# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application UI packages without request bindings and the platform asset route (F_08)."""

import asyncio
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
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderNode, RenderStatus, RenderTarget, UiActionOutput, UiRenderInput,
)

PACKAGE = "test.application-surface"
CLIENT_PACKAGE = "test.client-policy"
UI = {"apiVersion": 1, "items": [{"type": "web.slot.contribution", "slot": "application.sidebar.footer"}]}


def _channel():
    responses = []
    events = []
    sockets = {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = "browser-one"
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        events.append((event, payload))

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({"ok": ok, "payload": payload, "error": error, "code": code})

    return SimpleNamespace(
        channel_id="web", responses=responses, events=events, ensure_ws_id=ensure_ws_id,
        connection_user_id=lambda ws: None, ws_by_id=sockets.get,
        send_event=send_event, send_response=send_response,
    )


def _package_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "bg.bin").write_bytes(bytes(range(256)) * 4)
    (root / "secret.txt").write_text("not an asset")
    return root


async def _start(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(install_dir=tmp_path))
    await runtime.start()
    return runtime


def _publish(runtime: GatewayRuntime, root: Path, handlers, *, package_id: str = PACKAGE, client=None):
    runtime.publish(LoadedPackage(
        package_id=package_id, version="1.0.0", root=root, handlers=tuple(handlers),
        client=client or {}, ui=UI,
    ))


def _ready_params(package_id: str = "") -> dict:
    target = {"scope": "application", "slot_id": "application.sidebar.footer", "instance_key": "surface"}
    if package_id:
        target["package_id"] = package_id
    return {"event": "ui.ready", "target": target, "payload": {}}


async def test_package_without_client_receives_ready_and_click(tmp_path):
    seen = []

    async def ready(event, ops, next_handler):
        seen.append(("ready", event.event_input.target.package_id))
        return UiActionOutput(accepted=True, status="ready")

    async def click(event, ops, next_handler):
        seen.append(("click", event.event_input.payload["action_id"]))
        return UiActionOutput(accepted=True, status="applied")

    runtime = await _start(tmp_path)
    try:
        _publish(runtime, _package_root(tmp_path, "pkg"), [
            HandlerDeclaration("ui.ready", ready, {"target.package_id": PACKAGE}, "ready"),
            HandlerDeclaration("ui.click", click, {"target.package_id": PACKAGE}, "click"),
        ])
        ws = SimpleNamespace(closed=False)
        visited, _ = await runtime.intercept(ws, "ready", "webui.dispatch", _ready_params())
        assert visited is True
        assert runtime.channel.responses[-1]["payload"] == {"accepted": True, "status": "ready"}

        connection_id = await runtime.connection_id(ws)
        target = RenderTarget(session_id="", slot_id="application.sidebar.footer", package_id=PACKAGE,
                              instance_key="surface", scope="application", connection_id=connection_id)
        await runtime.bus.trigger(Event("ui.render", UiRenderInput(
            target=target,
            root=RenderNode(component="Button", props={"value": "Apply"}, events={"ui.click": {"id": "apply"}}),
        )), SessionContext("gateway:" + connection_id, None, {"connection_id": connection_id}))
        record = runtime.registry.get(target)
        params = {"event": "ui.click", "target": target.to_payload(), "payload": {
            "node_key": "0", "revision": record.revision, "action_id": "apply", "args": {}, "data": {},
        }}
        visited, _ = await runtime.intercept(ws, "click", "webui.dispatch", params)
        assert visited is True
        assert seen == [("ready", PACKAGE), ("click", "apply")]
        # Application packages are not declared to the browser and carry no frame generation.
        assert runtime.browser.frame_generation(PACKAGE) is None
        assert not any(event == "client.declarations" for event, _ in runtime.channel.events)
    finally:
        await runtime.shutdown()


async def test_background_package_renders_a_media_surface_from_its_assets(tmp_path):
    """A package contributing only application.background gets ready and renders MediaSurface (F_10)."""
    results = []

    async def ready(event, ops, next_handler):
        target = event.event_input.target
        src = await ops.ui.asset_url("bg.bin")
        for root in (
            RenderNode(component="MediaSurface", props={"kind": "image", "src": src, "title": "Theme"}),
            RenderNode(component="Box", props={}),
            RenderNode(component="MediaSurface", props={"kind": "image", "src": "https://example.com/bg.png",
                                                        "title": "Theme"}),
        ):
            results.append(await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key=target.instance_key),
                root=root,
            )))
        return UiActionOutput(accepted=True, status="ready")

    runtime = await _start(tmp_path)
    try:
        runtime.publish(LoadedPackage(
            package_id=PACKAGE, version="1.0.0", root=_package_root(tmp_path, "pkg"),
            handlers=(HandlerDeclaration("ui.ready", ready, {"target.package_id": PACKAGE}, "ready"),), client={},
            ui={"apiVersion": 1, "items": [{"type": "web.slot.contribution", "slot": "application.background"}]},
        ))
        ws = SimpleNamespace(closed=False)
        visited, _ = await runtime.intercept(ws, "ready", "webui.dispatch", {
            "event": "ui.ready", "payload": {},
            "target": {"scope": "application", "slot_id": "application.background", "instance_key": "main"},
        })
        assert visited is True
        assert runtime.channel.responses[-1]["payload"] == {"accepted": True, "status": "ready"}
        assert [result.status for result in results] == [
            RenderStatus.DELIVERED, RenderStatus.REJECTED, RenderStatus.REJECTED,
        ]
        record = runtime.registry.get(RenderTarget(
            session_id="", slot_id="application.background", package_id=PACKAGE, instance_key="main",
            scope="application", connection_id=await runtime.connection_id(ws),
        ))
        generation = runtime.browser.generations[PACKAGE]
        assert record.root.component == "MediaSurface"
        assert record.root.props["src"] == f"/extension-assets/{PACKAGE}/{generation}/bg.bin"
    finally:
        await runtime.shutdown()


async def test_package_without_client_gets_assets_but_not_browser_operations(tmp_path):
    results = {}

    async def ready(event, ops, next_handler):
        results["url"] = await ops.ui.asset_url("bg.bin")
        with pytest.raises(ValueError):
            await ops.client.local_storage_set("key", "value")
        with pytest.raises(ValueError):
            await ops.ui.asset_url("../secret.txt")
        return UiActionOutput(accepted=True, status="ready")

    runtime = await _start(tmp_path)
    try:
        _publish(runtime, _package_root(tmp_path, "pkg"), [
            HandlerDeclaration("ui.ready", ready, {"target.package_id": PACKAGE}, "ready"),
        ])
        await runtime.intercept(SimpleNamespace(closed=False), "ready", "webui.dispatch", _ready_params())
        generation = runtime.browser.generations[PACKAGE]
        assert results["url"] == f"/extension-assets/{PACKAGE}/{generation}/bg.bin"

        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(results["url"])
            ranged = await client.get(results["url"], headers={"Range": "bytes=10-19"})
            outside = await client.get(f"/extension-assets/{PACKAGE}/{generation}/..%2Fsecret.txt")
        assert response.status_code == 200
        assert response.content == bytes(range(256)) * 4
        assert response.headers["content-security-policy"] == "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert ranged.status_code == 206
        assert ranged.content == bytes(range(10, 20))
        assert outside.status_code == 404
    finally:
        await runtime.shutdown()


async def test_reload_retires_old_assets_and_late_renders(tmp_path):
    gate = asyncio.Event()
    delayed = []

    async def ready(event, ops, next_handler):
        target = event.event_input.target

        async def render_later():
            await gate.wait()
            return await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key="late"),
                root=RenderNode(component="Text", props={"content": "late"}),
            ))
        delayed.append(asyncio.ensure_future(render_later()))
        return UiActionOutput(accepted=True, status="ready")

    runtime = await _start(tmp_path)
    try:
        root = _package_root(tmp_path, "pkg")
        handlers = [HandlerDeclaration("ui.ready", ready, {"target.package_id": PACKAGE}, "ready")]
        _publish(runtime, root, handlers)
        ws = SimpleNamespace(closed=False)
        await runtime.intercept(ws, "ready", "webui.dispatch", _ready_params())
        old_generation = runtime.browser.generations[PACKAGE]

        _publish(runtime, root, handlers)
        assert runtime.browser.generations[PACKAGE] != old_generation
        gate.set()
        result = await delayed[0]
        assert result.status == RenderStatus.REJECTED

        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stale = await client.get(f"/extension-assets/{PACKAGE}/{old_generation}/bg.bin")
        assert stale.status_code == 404
    finally:
        await runtime.shutdown()


async def test_request_policy_decides_whether_assets_are_served(tmp_path):
    observed = []

    async def deny_all_but_assets(event, ops, next_handler):
        observed.append(event.event_input.path)
        if event.event_input.path.startswith("/extension-assets/"):
            return await next_handler(event)
        return GatewayRequestOutput(handled=True, route="deny", error={"code": "POLICY_DENIED", "status": 403})

    runtime = await _start(tmp_path)
    try:
        _publish(runtime, _package_root(tmp_path, "pkg"), [
            HandlerDeclaration("gateway.request", deny_all_but_assets, {}, "policy"),
        ], package_id=CLIENT_PACKAGE, client={"requestBindings": []})
        assert runtime.browser.enabled is True
        generation = runtime.browser.generations[CLIENT_PACKAGE]

        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)

        @app.get("/business")
        async def business():
            return {}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            asset = await client.get(f"/extension-assets/{CLIENT_PACKAGE}/{generation}/bg.bin")
            denied = await client.get("/business")
        assert asset.status_code == 200
        assert denied.status_code == 403
        assert observed == [f"/extension-assets/{CLIENT_PACKAGE}/{generation}/bg.bin", "/business"]
    finally:
        await runtime.shutdown()


async def test_packages_without_application_slots_stay_outside_the_bridge(tmp_path):
    runtime = await _start(tmp_path)
    try:
        runtime.publish(LoadedPackage(package_id="test.no-ui", version="1.0.0", root=tmp_path, handlers=()))
        assert runtime.browser.application_enabled is False
        assert "test.no-ui" not in runtime.browser.generations
        params = _ready_params()
        visited, returned = await runtime.intercept(SimpleNamespace(closed=False), "ready", "webui.dispatch", params)
        assert visited is False
        assert returned == params
    finally:
        await runtime.shutdown()
