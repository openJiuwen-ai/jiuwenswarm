# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-mediated file import and export for application UI packages (F_13)."""

import asyncio
import builtins
import json
import shutil
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from openjiuwen.core.common.exception.codes import StatusCode

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui import transfers as transfers_module
from jiuwenswarm.gateway.extensions_v2.webui.component_rules import validate_event_data
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes
from jiuwenswarm.gateway.extensions_v2.webui.schema import validate_target
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

PACKAGE = "test.theme"
SLOT = "application.settings"
LOCAL = ("127.0.0.1", 50000)
TARGET = RenderTarget(session_id="", slot_id=SLOT, package_id=PACKAGE, instance_key="main", scope="application",
                      connection_id="browser-1")


def _settings_items(package_id: str, instance_key: str = "main") -> list[dict]:
    """An S_18 settings menu with one application-scoped page (instance ``main`` by default)."""
    menu, section = f"{package_id}::menu", f"{package_id}::section"
    slot = {"type": "web.slot.contribution", "slot": "application.settings"}
    return [
        {**slot, "id": "menu", "kind": "menu", "location": {"after": "extensions"},
         "title": {"zh": "主题", "en": "Theme"}, "icon": "image"},
        {**slot, "id": "section", "kind": "section", "location": {"menu": menu}, "title": {"zh": "主题", "en": "Theme"}},
        {**slot, "id": "main", "location": {"menu": menu, "section": section}, "renderer": {"kind": "webui-tree"},
         "scope": "application", "instanceKey": instance_key},
    ]


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


class ThemePackage:
    """A settings page with an import control and an export button."""

    def __init__(self, picker: dict | None = None, instance_key: str = "main") -> None:
        self.picker = picker or {"label": "Import theme", "accept": [".zip"]}
        self.instance_key = instance_key
        self.imports: list = []
        self.kept: list = []
        self.offers: list = []
        self.export = "exports/backup.zip"
        self.filename = "备份 1.zip"

    def root(self) -> RenderNode:
        return RenderNode(component="Box", children=(
            RenderNode(component="FileImport", props=self.picker, events={"ui.change": {"id": "import"}}),
            RenderNode(component="Button", props={"value": "Export"}, events={"ui.click": {"id": "export"}}),
        ))

    async def ready(self, event, ops, next_handler):
        await ops.ui.render(UiRenderInput(target=RenderTarget(session_id="", slot_id=SLOT, instance_key=self.instance_key),
                                          root=self.root(), mode=RenderMode.REPLACE))
        return UiActionOutput(accepted=True, status="ready")

    async def change(self, event, ops, next_handler):
        data = dict(event.event_input.payload["data"])
        self.imports.append(data)
        root = await ops.data.root()
        for item in data["files"]:
            source = root / data["path"] / item["path"]
            self.kept.append(source.read_bytes())
        # Keep the first import by moving it out of the host directory.
        destination = root / "themes" / data["upload_id"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        (root / data["path"]).rename(destination)
        (root / data["path"]).mkdir()
        (root / data["path"] / "leftover.txt").write_text("host removes this")
        return UiActionOutput(accepted=True, status="imported")

    async def click(self, event, ops, next_handler):
        try:
            self.offers.append(await ops.ui.download(self.export, filename=self.filename))
        except ValueError as exc:
            self.offers.append(str(exc))
        return UiActionOutput(accepted=True, status="clicked")

    def loaded(self, root: Path) -> LoadedPackage:
        root.mkdir(parents=True, exist_ok=True)
        match = {"target.package_id": PACKAGE}
        return LoadedPackage(
            package_id=PACKAGE, version="1.0.0", root=root, client={},
            ui={"apiVersion": 1, "items": _settings_items(PACKAGE, self.instance_key)},
            handlers=(HandlerDeclaration("ui.ready", self.ready, match, "ready"),
                      HandlerDeclaration("ui.change", self.change, match, "change"),
                      HandlerDeclaration("ui.click", self.click, match, "click")),
        )


async def _start(tmp_path: Path, **config) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=tmp_path / "install", data_dir=tmp_path / "data", **config))
    await runtime.start()
    return runtime


async def _call(runtime, method, params, ws=None):
    await runtime.intercept(ws or SimpleNamespace(closed=False), "req", method, params)
    return runtime.channel.responses[-1]


async def _mount(runtime, package: ThemePackage, tmp_path: Path, ws=None) -> int:
    runtime.publish(package.loaded(tmp_path / "theme"))
    await _call(runtime, "webui.dispatch", {"event": "ui.ready", "payload": {}, "target": {
        "scope": "application", "slot_id": SLOT, "package_id": PACKAGE, "instance_key": package.instance_key}}, ws)
    record = runtime.registry.get(RenderTarget(session_id="", slot_id=SLOT, package_id=PACKAGE,
                                               instance_key=package.instance_key, scope="application",
                                               connection_id="browser-1"))
    return record.revision


def _begin(revision, files, node_key="0.0", instance_key="main"):
    return {"target": {"scope": "application", "slot_id": SLOT, "package_id": PACKAGE, "instance_key": instance_key},
            "node_key": node_key, "revision": revision, "files": files}


def _event(event, revision, node_key, action_id, data, instance_key="main"):
    return {"event": event, "target": {"scope": "application", "slot_id": SLOT, "package_id": PACKAGE,
                                       "instance_key": instance_key},
            "payload": {"node_key": node_key, "revision": revision, "action_id": action_id, "args": {}, "data": data}}


def _client(runtime, peer=LOCAL):
    app = FastAPI()
    attach_browser_routes(app, lambda: runtime)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url="http://test")


async def test_import_round_trip_gives_the_package_a_relative_path_and_cleans_up(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        body = bytes(range(256)) * 300
        response = await _call(runtime, "webui.upload.begin", _begin(revision, [{"path": "Aurora.ZIP", "size": len(body)}]))
        assert response["ok"] is True
        upload_id, url = response["payload"]["upload_id"], response["payload"]["url"]
        assert url == f"/extension-uploads/{upload_id}/"
        async with _client(runtime) as client:
            assert (await client.put(url + "Aurora.ZIP", content=body)).status_code == 204
            assert (await client.put(url + "Aurora.ZIP", content=body)).status_code == 409
        response = await _call(runtime, "webui.dispatch", _event("ui.change", revision, "0.0", "import",
                                                                 {"upload_id": upload_id}))
        assert response["payload"] == {"accepted": True, "status": "imported"}
        await runtime.browser.settle()
        assert package.imports == [{"upload_id": upload_id, "path": f".uploads/{upload_id}",
                                    "total_bytes": len(body), "files": [{"path": "Aurora.ZIP", "size": len(body)}]}]
        assert package.kept == [body]
        data = runtime.package_data_dir(PACKAGE)
        assert (data / "themes" / upload_id / "Aurora.ZIP").read_bytes() == body
        assert not (data / ".uploads" / upload_id).exists()
        # The ticket is single-use.
        response = await _call(runtime, "webui.dispatch", _event("ui.change", revision, "0.0", "import",
                                                                 {"upload_id": upload_id}))
        assert response["payload"]["accepted"] is False and len(package.imports) == 1
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("instance_key", ["settings:main", "主题 设置/页面 1", "k" * 200])
async def test_every_declarable_instance_key_renders_dispatches_and_imports(tmp_path, instance_key):
    """S_18 declarations, rendering, interaction and file import accept the same instance keys."""
    package = ThemePackage(instance_key=instance_key)
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        data = runtime.package_data_dir(PACKAGE)
        (data / "exports").mkdir(parents=True)
        (data / "exports" / "backup.zip").write_bytes(b"x")
        response = await _call(runtime, "webui.dispatch",
                               _event("ui.click", revision, "0.1", "export", {}, instance_key=instance_key))
        assert response["payload"] == {"accepted": True, "status": "clicked"}
        response = await _call(runtime, "webui.upload.begin",
                               _begin(revision, [{"path": "a.zip", "size": 1}], instance_key=instance_key))
        assert response["ok"] is True
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("instance_key", ["", "a|b", " main", "main ", "k" * 201, "\ud800"])
async def test_instance_keys_outside_the_shared_rule_are_refused_everywhere(tmp_path, instance_key):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        with pytest.raises(ValueError):
            runtime.browser.validate_package(ThemePackage(instance_key=instance_key).loaded(tmp_path / "other"))
        if not instance_key:
            return  # the empty key addresses the default instance of slots without declarations
        assert validate_target(RenderTarget(session_id="", slot_id=SLOT, package_id=PACKAGE, instance_key=instance_key,
                                            scope="application", connection_id="browser-1")).ok is False
        for slot in ("application.background", "application.splash"):
            response = await _call(runtime, "webui.dispatch", {"event": "ui.ready", "payload": {}, "target": {
                "scope": "application", "slot_id": slot, "package_id": PACKAGE, "instance_key": instance_key}})
            assert response["payload"] == {"accepted": False, "status": "invalid_target", "code": "SCHEMA_ERROR"}
        response = await _call(runtime, "webui.upload.begin",
                               _begin(revision, [{"path": "a.zip", "size": 1}], instance_key=instance_key))
        assert response["ok"] is False
    finally:
        await runtime.shutdown()


async def test_directory_import_writes_nested_files(tmp_path):
    package = ThemePackage({"label": "Import folder", "directory": True})
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        files = {"hive/theme.json": b"{}", "hive/media/bg.png": b"\x89PNG"}
        response = await _call(runtime, "webui.upload.begin", _begin(
            revision, [{"path": path, "size": len(body)} for path, body in files.items()]))
        upload_id, url = response["payload"]["upload_id"], response["payload"]["url"]
        async with _client(runtime) as client:
            for path, body in files.items():
                assert (await client.put(url + path, content=body)).status_code == 204
        response = await _call(runtime, "webui.dispatch", _event("ui.change", revision, "0.0", "import",
                                                                 {"upload_id": upload_id}))
        assert response["payload"]["accepted"] is True
        assert sorted(package.kept) == sorted(files.values())
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("picker,files,code", [
    (None, [], "BAD_REQUEST"),
    (None, [{"path": "a.zip", "size": 1}, {"path": "b.zip", "size": 1}], "BAD_REQUEST"),
    (None, [{"path": "a.txt", "size": 1}], "BAD_REQUEST"),
    (None, [{"path": "../a.zip", "size": 1}], "BAD_REQUEST"),
    (None, [{"path": "dir/a.zip", "size": 1}], "BAD_REQUEST"),
    (None, [{"path": "a.zip", "size": -1}], "BAD_REQUEST"),
    (None, [{"path": "a.zip", "size": True}], "BAD_REQUEST"),
    (None, [{"path": "a:b.zip", "size": 1}], "BAD_REQUEST"),
    (None, [{"path": "a.zip", "size": 1, "mode": 1}], "BAD_REQUEST"),
    (None, [{"path": "a.zip", "size": 2048}], "UPLOAD_TOO_LARGE"),
    ({"label": "Many", "multiple": True}, [{"path": "a.zip", "size": 1}, {"path": "A.zip", "size": 1}], "BAD_REQUEST"),
    ({"label": "Folder", "directory": True}, [{"path": "a.zip", "size": 1}], "BAD_REQUEST"),
    ({"label": "Folder", "directory": True}, [{"path": "x/a", "size": 1}, {"path": "y/b", "size": 1}], "BAD_REQUEST"),
    ({"label": "Folder", "directory": True}, [{"path": "x/a", "size": 1}, {"path": "x/a/b", "size": 1}], "BAD_REQUEST"),
    ({"label": "Off", "disabled": True}, [{"path": "a.zip", "size": 1}], "TARGET_INACTIVE"),
])
async def test_begin_rejects_manifests_outside_the_control(tmp_path, picker, files, code):
    package = ThemePackage(picker)
    runtime = await _start(tmp_path, upload_max_bytes=1024)
    try:
        revision = await _mount(runtime, package, tmp_path)
        response = await _call(runtime, "webui.upload.begin", _begin(revision, files))
        assert response["ok"] is False and response["code"] == code
        assert runtime.browser.transfers.uploads == {}
    finally:
        await runtime.shutdown()


async def test_begin_requires_the_current_import_node(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        files = [{"path": "a.zip", "size": 1}]
        for params in (_begin(revision + 1, files), _begin(revision, files, node_key="0.1"),
                       _begin(revision, files, node_key="0.9")):
            response = await _call(runtime, "webui.upload.begin", params)
            assert response["code"] == "TARGET_INACTIVE", params
        other = await _call(runtime, "webui.upload.begin", _begin(revision, files), SimpleNamespace(closed=False,
                                                                                                 name="browser-2"))
        assert other["code"] == "TARGET_INACTIVE"
        response = await _call(runtime, "webui.upload.begin", {**_begin(revision, files), "extra": 1})
        assert response["code"] == "BAD_REQUEST"
    finally:
        await runtime.shutdown()


async def test_transfer_route_serves_remote_peers_and_refuses_wrong_bodies(tmp_path):
    package = ThemePackage({"label": "Many", "multiple": True})
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        response = await _call(runtime, "webui.upload.begin", _begin(revision, [
            {"path": "a.zip", "size": 4}, {"path": "b.zip", "size": 4}, {"path": "c.zip", "size": 4}]))
        upload_id, url = response["payload"]["upload_id"], response["payload"]["url"]
        # A remote browser holding the ticket may upload (S_10 section 6.3).
        async with _client(runtime, ("203.0.113.7", 1)) as remote:
            assert (await remote.put(url + "b.zip", content=b"5678")).status_code == 204
        async with _client(runtime) as client:
            assert (await client.put(url + "d.zip", content=b"1234")).status_code == 404
            assert (await client.put("/extension-uploads/" + "0" * 32 + "/a.zip", content=b"1234")).status_code == 404
            assert (await client.put(url + "a.zip", content=b"12345")).status_code == 413
            assert (await client.put(url + "a.zip", content=b"12")).status_code == 400
            assert (await client.put(url + "a.zip", content=b"1234")).status_code == 204
        # Incomplete imports cannot be handed to the package.
        response = await _call(runtime, "webui.dispatch", _event("ui.change", revision, "0.0", "import",
                                                                 {"upload_id": upload_id}))
        assert response["payload"]["accepted"] is False and package.imports == []
        directory = runtime.package_data_dir(PACKAGE) / ".uploads" / upload_id
        assert sorted(path.name for path in directory.iterdir()) == ["a.zip", "b.zip"]
    finally:
        await runtime.shutdown()


async def test_tickets_expire_and_close_with_the_connection(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    clock = [1000.0]
    runtime.browser.transfers._clock = lambda: clock[0]
    try:
        ws = SimpleNamespace(closed=False)
        revision = await _mount(runtime, package, tmp_path, ws)
        files = [{"path": "a.zip", "size": 1}]
        first = (await _call(runtime, "webui.upload.begin", _begin(revision, files), ws))["payload"]["upload_id"]
        uploads = runtime.package_data_dir(PACKAGE) / ".uploads"
        clock[0] += transfers_module.UPLOAD_TTL_S + 1
        async with _client(runtime) as client:
            assert (await client.put(f"/extension-uploads/{first}/a.zip", content=b"1")).status_code == 404
        assert not (uploads / first).exists()
        opened = [(await _call(runtime, "webui.upload.begin", _begin(revision, files), ws))["payload"]["upload_id"]
                  for _ in range(transfers_module.MAX_OPEN_UPLOADS + 1)]
        assert sorted(runtime.browser.transfers.uploads) == sorted(opened[1:])
        assert not (uploads / opened[0]).exists()
        await runtime.on_disconnect(ws)
        await runtime.browser.settle()
        assert runtime.browser.transfers.uploads == {} and list(uploads.iterdir()) == []
    finally:
        await runtime.shutdown()


async def test_concurrent_expiry_removes_each_ticket_once(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    transfers = runtime.browser.transfers
    clock = [1000.0]
    transfers._clock = lambda: clock[0]
    discard, gate, removed = transfers._discard, asyncio.Event(), []

    async def slow_discard(upload):
        removed.append(upload.upload_id)
        await gate.wait()
        await discard(upload)

    try:
        ws = SimpleNamespace(closed=False)
        revision = await _mount(runtime, package, tmp_path, ws)
        files = [{"path": "a.zip", "size": 1}]
        opened = [(await _call(runtime, "webui.upload.begin", _begin(revision, files), ws))["payload"]["upload_id"]
                  for _ in range(2)]
        clock[0] += transfers_module.UPLOAD_TTL_S + 1
        transfers._discard = slow_discard
        first, second = asyncio.create_task(transfers.expire()), asyncio.create_task(transfers.expire())
        await asyncio.sleep(0)
        assert transfers.uploads == {}, "records leave before any directory is deleted"
        gate.set()
        await asyncio.gather(first, second)
        assert sorted(removed) == sorted(opened)
        uploads = runtime.package_data_dir(PACKAGE) / ".uploads"
        assert not any((uploads / upload_id).exists() for upload_id in opened)
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_cancelled_upload_open_closes_the_worker_handle_and_removes_the_partial(tmp_path, monkeypatch, cancel_twice):
    runtime = await _start(tmp_path)
    transfers = runtime.browser.transfers
    entered, finished = asyncio.Event(), asyncio.Event()
    resume = threading.Event()
    loop, opened, call = asyncio.get_running_loop(), {}, None

    def slow_open(path, mode):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not resume.wait(5):
                raise TimeoutError("upload test worker was not released")
            handle = builtins.open(path, mode)
            opened.update(handle=handle, path=path)
            return handle
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def content():
        yield b"x"

    try:
        revision = await _mount(runtime, ThemePackage(), tmp_path)
        ticket = await transfers.begin_upload(SimpleNamespace(
            params=_begin(revision, [{"path": "one.zip", "size": 1}]), connection_id="browser-1"))
        monkeypatch.setattr(transfers_module, "open", slow_open, raising=False)
        call = asyncio.create_task(transfers.receive(ticket["upload_id"], "one.zip", content()))
        await asyncio.wait_for(entered.wait(), 2)
        call.cancel()
        await asyncio.sleep(0)
        if cancel_twice:
            call.cancel()
            await asyncio.sleep(0)
        manager = GatewayPackageManager(runtime, tmp_path / "install")
        report = await manager.uninstall(PACKAGE)
        assert report.status == "FAILED"
        assert report.error.status is StatusCode.EXTENSION_PACKAGE_BUSY
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(finished.wait(), 2)
        assert opened["handle"].closed, "cancellation must not lose the handle returned by the worker"
        assert not opened["path"].exists()
        assert transfers.uploads[ticket["upload_id"]].receiving == set()
        assert (await manager.uninstall(PACKAGE)).status == "SUCCESS"
        assert not runtime.package_data_dir(PACKAGE).exists()
    finally:
        resume.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 2)
        if "handle" in opened:
            opened["handle"].close()
        await runtime.shutdown()


async def test_cancelled_import_preparation_finishes_its_directory_cleanup_before_uninstall(tmp_path, monkeypatch):
    runtime = await _start(tmp_path)
    started, finished, resume = asyncio.Event(), asyncio.Event(), threading.Event()
    loop, mkdir, created = asyncio.get_running_loop(), Path.mkdir, []
    call = None

    def slow_mkdir(path, *args, **kwargs):
        if path.parent.name != ".uploads":
            return mkdir(path, *args, **kwargs)
        created.append(path)
        loop.call_soon_threadsafe(started.set)
        try:
            if not resume.wait(5):
                raise TimeoutError("import preparation test worker was not released")
            return mkdir(path, *args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    try:
        revision = await _mount(runtime, ThemePackage(), tmp_path)
        monkeypatch.setattr(Path, "mkdir", slow_mkdir)
        call = asyncio.create_task(runtime.browser.transfers.begin_upload(SimpleNamespace(
            params=_begin(revision, [{"path": "one.zip", "size": 1}]), connection_id="browser-1")))
        await asyncio.wait_for(started.wait(), 2)
        call.cancel()
        await asyncio.sleep(0)
        manager = GatewayPackageManager(runtime, tmp_path / "install")
        report = await manager.uninstall(PACKAGE)
        assert report.error.status is StatusCode.EXTENSION_PACKAGE_BUSY
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(finished.wait(), 2)
        assert runtime.browser.transfers.uploads == {} and not created[0].exists()
        assert (await manager.uninstall(PACKAGE)).status == "SUCCESS"
        assert not runtime.package_data_dir(PACKAGE).exists()
    finally:
        resume.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        if started.is_set():
            await asyncio.wait_for(finished.wait(), 2)
        await runtime.shutdown()


@pytest.mark.parametrize("phase", ["expiry", "directory"])
@pytest.mark.parametrize("change", ["reload", "rerender", "disconnect"])
async def test_import_preparation_refuses_a_target_changed_while_waiting(tmp_path, monkeypatch, phase, change):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    transfers = runtime.browser.transfers
    ws = SimpleNamespace(closed=False)
    expire, to_thread = transfers.expire, asyncio.to_thread

    async def change_target():
        if change == "reload":
            await _mount(runtime, ThemePackage({"label": "Images only", "accept": [".png"]}), tmp_path, ws)
        elif change == "rerender":
            package.picker = {"label": "Images only", "accept": [".png"]}
            await _call(runtime, "webui.dispatch", {"event": "ui.ready", "payload": {}, "target": {
                "scope": "application", "slot_id": SLOT, "package_id": PACKAGE, "instance_key": "main"}}, ws)
        else:
            await runtime.on_disconnect(ws)

    async def expire_then_change():
        transfers.expire = expire
        await expire()
        await change_target()

    async def make_directory_then_change(operation, *args, **kwargs):
        result = await to_thread(operation, *args, **kwargs)
        method = operation.func if isinstance(operation, partial) else operation
        path = getattr(method, "__self__", None)
        if getattr(method, "__name__", "") == "mkdir" and isinstance(path, Path) and path.parent.name == ".uploads":
            await change_target()
        return result

    try:
        revision = await _mount(runtime, package, tmp_path, ws)
        if phase == "expiry":
            monkeypatch.setattr(transfers, "expire", expire_then_change)
        else:
            monkeypatch.setattr(asyncio, "to_thread", make_directory_then_change)
        with pytest.raises(transfers_module.HostRequestError) as caught:
            await transfers.begin_upload(SimpleNamespace(
                params=_begin(revision, [{"path": "old.zip", "size": 1}]), connection_id="browser-1"))
        assert caught.value.code == "TARGET_INACTIVE"
        assert transfers.uploads == {}
        await runtime.browser.settle()
        directory = runtime.package_data_dir(PACKAGE) / ".uploads"
        assert not directory.exists() or list(directory.iterdir()) == []
        if change != "disconnect":
            monkeypatch.setattr(transfers, "expire", expire)
            monkeypatch.setattr(asyncio, "to_thread", to_thread)
            current = runtime.registry.get(TARGET)
            fresh = await transfers.begin_upload(SimpleNamespace(
                params=_begin(current.revision, [{"path": "new.png", "size": 1}]), connection_id="browser-1"))

            async def content():
                yield b"x"

            assert await transfers.receive(fresh["upload_id"], "new.png", content()) == 204
            accepted = await _call(runtime, "webui.dispatch", _event(
                "ui.change", current.revision, "0.0", "import", {"upload_id": fresh["upload_id"]}), ws)
            assert accepted["payload"] == {"accepted": True, "status": "imported"}
    finally:
        await runtime.shutdown()


async def test_concurrent_imports_of_one_connection_respect_the_open_ticket_limit(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        ws = SimpleNamespace(closed=False)
        revision = await _mount(runtime, package, tmp_path, ws)
        files = [{"path": "a.zip", "size": 1}]
        count = transfers_module.MAX_OPEN_UPLOADS + 3
        results = await asyncio.gather(*(runtime.browser.transfers.begin_upload(SimpleNamespace(
            params=_begin(revision, files), connection_id="browser-1")) for _ in range(count)), return_exceptions=True)
        tickets = runtime.browser.transfers.uploads
        assert len(tickets) == transfers_module.MAX_OPEN_UPLOADS
        issued = [result["upload_id"] for result in results if isinstance(result, dict)]
        refused = [result for result in results if not isinstance(result, dict)]
        # Requests replaced by newer ones before their directory existed are refused, not handed a dead ticket.
        assert set(tickets) <= set(issued)
        assert all(getattr(error, "code", None) == transfers_module.CODE_TARGET_INACTIVE for error in refused)
        uploads = runtime.package_data_dir(PACKAGE) / ".uploads"
        await runtime.browser.settle()
        assert sorted(path.name for path in uploads.iterdir()) == sorted(tickets)
    finally:
        await runtime.shutdown()


async def test_start_removes_imports_left_by_an_earlier_process(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    leftover = runtime.package_data_dir(PACKAGE) / ".uploads" / ("f" * 32)
    leftover.mkdir(parents=True)
    (leftover / "a.zip").write_bytes(b"old")
    await runtime.shutdown()
    install = tmp_path / "install" / PACKAGE / "1.0.0"
    (install / "theme").mkdir(parents=True)
    (install / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1, "package_type": "extension",
        "package": {"id": PACKAGE, "version": "1.0.0", "domain": "gateway"},
        "extensions": [{"id": "theme", "runtime": "backend", "module": "theme/extension.py"}],
        "ui": {"apiVersion": 1, "items": _settings_items(PACKAGE)},
    }), encoding="utf-8")
    (install / "theme" / "extension.py").write_text(
        "class Extension:\n    def register(self, registrar):\n        pass\n\n\n"
        "def create_extension():\n    return Extension()\n", encoding="utf-8")
    restarted = await _start(tmp_path)
    try:
        assert restarted.is_active_package(PACKAGE)
        assert not (restarted.package_data_dir(PACKAGE) / ".uploads").exists()
    finally:
        await restarted.shutdown()
    del package


async def test_download_is_single_use_for_the_calling_connection(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        data = runtime.package_data_dir(PACKAGE)
        (data / "exports").mkdir(parents=True)
        (data / "exports" / "backup.zip").write_bytes(b"backup-bytes")
        response = await _call(runtime, "webui.dispatch", _event("ui.click", revision, "0.1", "export", {}))
        assert response["payload"]["accepted"] is True and package.offers == [{"status": "offered"}]
        connection, event, payload = runtime.channel.events[-1]
        assert (connection, event) == ("browser-1", "webui.download")
        assert payload["package_id"] == PACKAGE and payload["filename"] == "备份 1.zip" and payload["size"] == 12
        url = payload["url"]
        assert url.startswith("/extension-downloads/")
        async with _client(runtime, ("203.0.113.7", 1)) as remote:
            # The connection that asked may be a remote browser; HEAD does not use the offer up.
            assert (await remote.head(url)).status_code == 200
            answer = await remote.get(url)
            assert answer.status_code == 200 and answer.content == b"backup-bytes"
            assert answer.headers["content-type"] == "application/octet-stream"
            assert answer.headers["content-disposition"] == "attachment; filename*=UTF-8''%E5%A4%87%E4%BB%BD%201.zip"
            assert answer.headers["cache-control"] == "no-store"
            assert answer.headers["x-content-type-options"] == "nosniff"
            assert answer.headers["content-security-policy"] == "sandbox"
        async with _client(runtime) as client:
            assert (await client.get(url)).status_code == 404
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("export,filename", [
    ("../outside.zip", None), ("/etc/passwd", None), ("exports", None), ("exports/missing.zip", None),
    ("link/backup.zip", None), ("exports/backup.zip", "/"), ("exports/backup.zip", 3),
])
async def test_download_refuses_paths_outside_the_data_directory(tmp_path, export, filename):
    package = ThemePackage()
    package.export, package.filename = export, filename
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        data = runtime.package_data_dir(PACKAGE)
        (data / "exports").mkdir(parents=True)
        (data / "exports" / "backup.zip").write_bytes(b"x")
        (tmp_path / "outside.zip").write_bytes(b"secret")
        (data / "link").symlink_to(data / "exports", target_is_directory=True)
        await _call(runtime, "webui.dispatch", _event("ui.click", revision, "0.1", "export", {}))
        assert len(package.offers) == 1 and isinstance(package.offers[0], str)
        assert not any(event == "webui.download" for _, event, _ in runtime.channel.events)
    finally:
        await runtime.shutdown()


async def test_a_release_while_the_download_is_prepared_sends_no_offer(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        data = runtime.package_data_dir(PACKAGE)
        (data / "exports").mkdir(parents=True)
        (data / "exports" / "backup.zip").write_bytes(b"x")
        transfers = runtime.browser.transfers
        expire = transfers.expire

        async def release_meanwhile():
            await expire()
            runtime.unpublish(PACKAGE)  # uninstall lands while the offer waits for expired tickets

        transfers.expire = release_meanwhile
        await _call(runtime, "webui.dispatch", _event("ui.click", revision, "0.1", "export", {}))
        assert len(package.offers) == 1 and isinstance(package.offers[0], str)
        assert not any(event == "webui.download" for _, event, _ in runtime.channel.events)
        assert transfers.downloads == {}
    finally:
        await runtime.shutdown()


async def test_release_invalidates_tickets_and_downloads(tmp_path):
    package = ThemePackage()
    runtime = await _start(tmp_path)
    try:
        revision = await _mount(runtime, package, tmp_path)
        data = runtime.package_data_dir(PACKAGE)
        (data / "exports").mkdir(parents=True)
        (data / "exports" / "backup.zip").write_bytes(b"x")
        upload_id = (await _call(runtime, "webui.upload.begin", _begin(revision, [{"path": "a.zip", "size": 1}])))[
            "payload"]["upload_id"]
        await _call(runtime, "webui.dispatch", _event("ui.click", revision, "0.1", "export", {}))
        url = runtime.channel.events[-1][2]["url"]
        runtime.unpublish(PACKAGE)
        await runtime.browser.settle()
        assert not (data / ".uploads" / upload_id).exists()
        async with _client(runtime) as client:
            assert (await client.get(url)).status_code == 404
            assert (await client.put(f"/extension-uploads/{upload_id}/a.zip", content=b"1")).status_code == 404
    finally:
        await runtime.shutdown()
    shutil.rmtree(tmp_path / "data", ignore_errors=True)


@pytest.mark.parametrize("data,allowed", [
    ({"upload_id": "a" * 32}, True), ({"upload_id": "A" * 32}, False), ({"upload_id": "a" * 31}, False),
    ({"upload_id": "../x"}, False), ({"upload_id": 7}, False), ({}, False), ({"upload_id": "a" * 32, "path": "x"}, False),
])
def test_file_import_change_data_carries_only_a_host_upload_id(data, allowed):
    assert validate_event_data("FileImport", "ui.change", data, {"label": "Import"}) is allowed
    assert validate_event_data("FileImport", "ui.change", {"upload_id": "a" * 32}, {"label": "x", "disabled": True}) is False
