# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application settings page, host methods and splash preferences (F_13)."""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui import host_settings
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

SURFACE = RenderNode(component="MediaSurface", props={"kind": "html", "src": "/extension-assets/p/g/boot.html",
                                                      "title": "Boot"})
PAGE = RenderNode(component="Box", children=(RenderNode(component="Text", props={"value": "Theme settings"}),))
READY = {"launch_id": "launch-1", "first_visit": False, "reduced_motion": False}
AUTH = {"authorization": "Bearer tk"}


def _channel():
    responses, events, sockets = [], [], {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = getattr(ws, "name", "browser-1")
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        events.append((ws._jiuwen_ws_id, event, payload))

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({"ok": ok, "payload": payload, "error": error, "code": code})

    return SimpleNamespace(channel_id="web", responses=responses, events=events, ensure_ws_id=ensure_ws_id,
                           connection_user_id=lambda ws: None, ws_by_id=sockets.get,
                           send_event=send_event, send_response=send_response)


class Package:
    """A Gateway package with splash and settings contributions that records lifecycle events."""

    def __init__(self, package_id: str, slots=("application.splash",), *, title: str = "") -> None:
        self.package_id = package_id
        self.slots = slots
        self.title = title
        self.readies: list = []
        self.closed: list = []

    async def ready(self, event, ops, next_handler):
        target = event.event_input.target
        self.readies.append((target.slot_id, dict(event.event_input.payload)))
        root = SURFACE if target.slot_id == "application.splash" else PAGE
        await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key=target.instance_key),
            root=root, mode=RenderMode.REPLACE,
        ))
        return UiActionOutput(accepted=True, status="ready")

    async def on_closed(self, event, ops, next_handler):
        self.closed.append(dict(event.event_input.payload))
        return UiActionOutput(accepted=True, status="closed")

    def loaded(self, root: Path) -> LoadedPackage:
        root.mkdir(parents=True, exist_ok=True)
        match = {"target.package_id": self.package_id}
        return LoadedPackage(
            package_id=self.package_id, version="1.0.0", root=root, client={}, display_name=self.title,
            ui={"apiVersion": 1, "items": [{"type": "web.slot.contribution", "slot": slot} for slot in self.slots]},
            handlers=(HandlerDeclaration("ui.ready", self.ready, match, "ready"),
                      HandlerDeclaration("ui.closed", self.on_closed, match, "closed")),
        )


def _policy(root: Path, *, deny: bool = False, require_login: bool = False) -> LoadedPackage:
    async def handle(event, ops, next_handler):
        anonymous = event.event_input.headers.get("authorization") != AUTH["authorization"]
        if deny or (require_login and anonymous):
            return GatewayRequestOutput(handled=True, route="deny", error={"code": "AUTH_REQUIRED", "message": "login"})
        return await next_handler(event)
    root.mkdir(parents=True, exist_ok=True)
    return LoadedPackage(package_id="test.policy", version="1.0.0", root=root, client={}, ui={},
                         handlers=(HandlerDeclaration("gateway.request", handle, {}, "policy"),))


async def _start(tmp_path: Path, install_dir: Path | None = None) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=install_dir or tmp_path / "install", data_dir=tmp_path / "data"))
    await runtime.start()
    return runtime


async def _call(runtime, method, params=None, *, ws=None, headers=None):
    ws = ws or SimpleNamespace(closed=False)
    visited, _ = await runtime.intercept(ws, "req", method, params or {}, headers=headers)
    return visited, runtime.channel.responses[-1]


def _dispatch(slot, event, package_id, payload=None, instance_key="main"):
    return {"event": event, "payload": payload if payload is not None else {},
            "target": {"scope": "application", "slot_id": slot, "package_id": package_id,
                       "instance_key": instance_key}}


async def test_settings_pages_list_declaring_packages_with_their_titles(tmp_path):
    theme = Package("test.theme", ("application.settings.page", "application.background"), title="Theme Studio")
    plain = Package("test.plain", ("application.background",))
    untitled = Package("test.untitled", ("application.settings.page",))
    runtime = await _start(tmp_path)
    try:
        for package in (theme, plain, untitled):
            runtime.publish(package.loaded(tmp_path / package.package_id))
        visited, response = await _call(runtime, "webui.settings.pages")
        assert visited is True and response["ok"] is True
        assert response["payload"] == {"pages": [
            {"package_id": "test.theme", "title": "Theme Studio"},
            {"package_id": "test.untitled", "title": "test.untitled"},
        ]}
        _, response = await _call(runtime, "webui.dispatch", _dispatch("application.settings.page", "ui.ready",
                                                                        "test.theme"))
        assert response["payload"]["accepted"] is True
        assert theme.readies == [("application.settings.page", {})] and untitled.readies == []
        target = RenderTarget(session_id="", slot_id="application.settings.page", package_id="test.theme",
                              instance_key="main", scope="application", connection_id="browser-1")
        assert runtime.registry.get(target).root == PAGE
        _, response = await _call(runtime, "webui.settings.pages", {"extra": 1})
        assert response["ok"] is False and response["code"] == "BAD_REQUEST"
    finally:
        await runtime.shutdown()


async def test_host_methods_are_answered_without_user_handlers(tmp_path):
    runtime = await _start(tmp_path)
    try:
        assert runtime.active is False
        visited, response = await _call(runtime, "webui.settings.pages")
        assert visited is True and response["payload"] == {"pages": []}
        visited, response = await _call(runtime, "webui.splash.settings")
        assert visited is True and response["payload"] == {"providers": [], "customized": False}
    finally:
        await runtime.shutdown()


async def test_host_methods_follow_the_request_policy(tmp_path):
    theme = Package("test.theme", ("application.settings.page",))
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.loaded(tmp_path / "theme"))
        runtime.publish(_policy(tmp_path / "login", require_login=True))
        # A USER policy that requires a login guards the host methods as well.
        for method in ("webui.settings.pages", "webui.splash.settings", "webui.splash.settings.update",
                       "webui.upload.begin"):
            visited, response = await _call(runtime, method, {"order": None})
            assert visited is True and response["ok"] is False and response["code"] == "AUTH_REQUIRED", method
        _, response = await _call(runtime, "webui.settings.pages", headers=AUTH)
        assert response["ok"] is True and response["payload"]["pages"][0]["package_id"] == "test.theme"
        # A USER request policy decides before the host does.
        runtime.publish(_policy(tmp_path / "deny", deny=True))
        _, response = await _call(runtime, "webui.settings.pages", headers=AUTH)
        assert response["ok"] is False and response["code"] == "AUTH_REQUIRED"
    finally:
        await runtime.shutdown()


async def test_splash_order_disable_and_reset_are_saved_across_restarts(tmp_path):
    packages = [Package(f"test.p{index}") for index in (1, 2, 3)]
    runtime = await _start(tmp_path)
    try:
        for package in packages:
            runtime.publish(package.loaded(tmp_path / package.package_id))
        _, response = await _call(runtime, "webui.splash.settings")
        assert [item["package_id"] for item in response["payload"]["providers"]] == ["test.p1", "test.p2", "test.p3"]
        assert response["payload"]["providers"][0] == {"package_id": "test.p1", "title": "test.p1", "skippable": False,
                                                      "max_duration_ms": 60000, "enabled": True}
        _, response = await _call(runtime, "webui.splash.settings.update", {"order": ["test.p3", "test.p1"]})
        assert [item["package_id"] for item in response["payload"]["providers"]] == ["test.p3", "test.p1", "test.p2"]
        assert response["payload"]["customized"] is True
        _, response = await _call(runtime, "webui.splash.settings.update", {"disabled": ["test.p2"]})
        assert [item["enabled"] for item in response["payload"]["providers"]] == [True, True, False]
        _, response = await _call(runtime, "webui.splash.providers")
        assert [item["package_id"] for item in response["payload"]["providers"]] == ["test.p3", "test.p1"]
        assert response["payload"]["customized"] is True
        for params in ({}, {"order": ["test.unknown"]}, {"order": ["test.p1", "test.p1"]}, {"order": "test.p1"},
                       {"disabled": "test.p1"}, {"disabled": ["test.unknown"]}, {"extra": True}):
            _, response = await _call(runtime, "webui.splash.settings.update", params)
            assert response["ok"] is False and response["code"] == "BAD_REQUEST", params
    finally:
        await runtime.shutdown()

    saved = json.loads((tmp_path / "data" / "host" / "splash.json").read_text(encoding="utf-8"))
    assert saved == {"version": 1, "installed": ["test.p1", "test.p2", "test.p3"],
                     "order": ["test.p3", "test.p1"], "disabled": ["test.p2"]}
    restarted = await _start(tmp_path)
    try:
        for package in reversed(packages):
            restarted.publish(package.loaded(tmp_path / package.package_id))
        _, response = await _call(restarted, "webui.splash.providers")
        assert [item["package_id"] for item in response["payload"]["providers"]] == ["test.p3", "test.p1"]
        _, response = await _call(restarted, "webui.splash.settings.update", {"order": None, "disabled": []})
        # Resetting returns to install order, not to this start's publication order.
        assert [item["package_id"] for item in response["payload"]["providers"]] == ["test.p1", "test.p2", "test.p3"]
        assert response["payload"]["customized"] is False
    finally:
        await restarted.shutdown()


async def test_unreadable_preferences_count_as_empty(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(host_settings.logger, "warning", lambda message, *args: warnings.append(message % args))
    (tmp_path / "data" / "host").mkdir(parents=True)
    (tmp_path / "data" / "host" / "splash.json").write_text('{"version": 1, "installed": "x"}', encoding="utf-8")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(Package("test.p1").loaded(tmp_path / "p1"))
        _, response = await _call(runtime, "webui.splash.providers")
        assert response["payload"] == {"providers": [{"package_id": "test.p1", "skippable": False,
                                                      "max_duration_ms": 60000}], "customized": False}
        assert len(warnings) == 1 and "ignore splash preferences" in warnings[0]
    finally:
        await runtime.shutdown()


async def test_disabling_a_playing_splash_closes_it_once(tmp_path):
    provider, other = Package("test.play"), Package("test.other")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(provider.loaded(tmp_path / "play"))
        runtime.publish(other.loaded(tmp_path / "other"))
        ws = SimpleNamespace(closed=False)
        _, response = await _call(runtime, "webui.dispatch", _dispatch("application.splash", "ui.ready", "test.play",
                                                                        READY), ws=ws)
        assert response["payload"] == {"accepted": True, "status": "playing"}
        target = RenderTarget(session_id="", slot_id="application.splash", package_id="test.play", instance_key="main",
                              scope="application", connection_id="browser-1")
        revision = runtime.registry.get(target).revision
        _, response = await _call(runtime, "webui.splash.settings.update", {"disabled": ["test.play"]})
        assert response["ok"] is True
        assert provider.closed == [{"reason": "disabled"}] and other.closed == []
        assert runtime.registry.get(target) is None
        clears = [payload for _, event, payload in runtime.channel.events if event == "webui.render.clear"]
        assert clears == [{"target": target.to_payload(), "revision": revision + 1, "reason": "disabled"}]
        _, response = await _call(runtime, "webui.dispatch", _dispatch(
            "application.splash", "ui.closed", "test.play", {"reason": "skip", "revision": revision}), ws=ws)
        assert response["payload"]["status"] == "inactive" and len(provider.closed) == 1
        # Disabling it again does not notify twice.
        await _call(runtime, "webui.splash.settings.update", {"disabled": ["test.play"]})
        assert len(provider.closed) == 1
    finally:
        await runtime.shutdown()


SPLASH_ENTRY = """\
from pathlib import Path

from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

LOG = Path({log!r})


class Extension:
    def register(self, registrar):
        async def ready(event, ops, next_handler):
            target = event.event_input.target
            await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key=target.instance_key),
                root=RenderNode(component="MediaSurface", props={{
                    "kind": "html", "src": "/extension-assets/p/g/boot.html", "title": "Boot"}}),
                mode=RenderMode.REPLACE,
            ))
            return UiActionOutput(accepted=True, status="ready")

        async def closed(event, ops, next_handler):
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(event.event_input.payload["reason"] + "\\n")
            return UiActionOutput(accepted=True, status="closed")

        match = {{"target.package_id": "demo.splash"}}
        registrar.register_event_handler("ui.ready", ready, match, "user")
        registrar.register_event_handler("ui.closed", closed, match, "user")


def create_extension():
    return Extension()
"""


def _splash_zip(tmp_path: Path, log: Path, version: str) -> Path:
    source = tmp_path / "src" / version
    (source / "demo_splash").mkdir(parents=True)
    (source / "manifest.yaml").write_text(
        "schemaVersion: 1\npackage:\n  id: demo.splash\n  version: " + version + "\ndomain: gateway\n"
        "extensions:\n  - id: demo\n    runtime: backend\n    module: demo_splash/extension.py\n"
        "ui:\n  apiVersion: 1\n  items:\n    - type: web.slot.contribution\n      slot: application.splash\n",
        encoding="utf-8",
    )
    (source / "demo_splash" / "extension.py").write_text(SPLASH_ENTRY.format(log=str(log)), encoding="utf-8")
    archive = tmp_path / f"demo-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in source.rglob("*"):
            bundle.write(path, path.relative_to(source).as_posix())
    return archive


async def _play(runtime) -> RenderTarget:
    await _call(runtime, "webui.dispatch", _dispatch("application.splash", "ui.ready", "demo.splash", READY))
    target = RenderTarget(session_id="", slot_id="application.splash", package_id="demo.splash", instance_key="main",
                          scope="application", connection_id="browser-1")
    assert runtime.registry.get(target) is not None
    return target


async def test_reload_upgrade_and_uninstall_close_a_playing_splash_as_removed(tmp_path):
    log = tmp_path / "closed.log"
    runtime = await _start(tmp_path, install_dir=tmp_path / "pkgs")
    manager = GatewayPackageManager(runtime, tmp_path / "pkgs")
    try:
        reports = await manager.install([_splash_zip(tmp_path, log, "1.0.0")])
        assert reports[0].status == "SUCCESS"
        target = await _play(runtime)
        assert (await manager.reload("demo.splash")).status == "SUCCESS"
        await runtime.browser.settle()
        assert log.read_text(encoding="utf-8").split() == ["removed"] and runtime.registry.get(target) is None
        await _play(runtime)
        assert (await manager.install([_splash_zip(tmp_path, log, "1.1.0")]))[0].status == "SUCCESS"
        await _play(runtime)
        assert (await manager.uninstall("demo.splash")).status == "SUCCESS"
        assert log.read_text(encoding="utf-8").split() == ["removed", "removed", "removed"]
        clears = [payload["reason"] for _, event, payload in runtime.channel.events if event == "webui.render.clear"]
        assert clears.count("removed") == 3
        saved = json.loads((tmp_path / "data" / "host" / "splash.json").read_text(encoding="utf-8"))
        assert saved["installed"] == []
    finally:
        await runtime.shutdown()
