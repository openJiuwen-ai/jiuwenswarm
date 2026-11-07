# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway package store, data directory and data assets (F_09)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from openjiuwen.core.common.exception.codes import StatusCode

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig, load_gateway_config
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes
from jiuwenswarm.gateway.extensions_v2.webui.models import UiActionOutput

UI = {"apiVersion": 1, "items": [{"type": "web.slot.contribution", "slot": "application.sidebar.footer"}]}


def _channel():
    responses = []

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = "browser-one"
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        pass

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code})

    def register_method(name, handler):
        pass

    return SimpleNamespace(
        channel_id="web", responses=responses, ensure_ws_id=ensure_ws_id, connection_user_id=lambda ws: None,
        ws_by_id=lambda ws_id: None, send_event=send_event, send_response=send_response,
        register_method=register_method,
    )


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=tmp_path / "extension_pkgs", data_dir=tmp_path / "gateway-data",
    ))
    await runtime.start()
    return runtime


def _store_package(package_id: str, root: Path) -> LoadedPackage:
    """A package that runs store commands named ``<package_id>:<op>:<key>[:<json value>]``."""

    async def command(event, ops, next_handler):
        method = event.event_input.method
        if not method.startswith(package_id + ":"):
            return await next_handler(event)
        _, op, key, *value = method.split(":", 3)
        if op == "set":
            result = await ops.store.set(key, json.loads(value[0]))
        elif op == "get":
            result = await ops.store.get(key, "missing")
        elif op == "delete":
            result = await ops.store.delete(key)
        else:
            result = await ops.store.keys()
        return GatewayRequestOutput(handled=True, route="consume", response={"result": result})

    return LoadedPackage(package_id=package_id, version="1.0.0", root=root,
                         handlers=(HandlerDeclaration("gateway.request", command, {}, "command"),))


async def _run(runtime: GatewayRuntime, method: str):
    await runtime.intercept(SimpleNamespace(closed=False), method, method, {})
    return runtime.channel.responses[-1]["payload"]["result"]


async def test_store_is_per_package_and_survives_reload_and_restart(tmp_path):
    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(_store_package("test.alpha", tmp_path))
        runtime.publish(_store_package("test.beta", tmp_path))
        assert await _run(runtime, 'test.alpha:set:theme:"hive"') is True
        assert await _run(runtime, 'test.beta:set:theme:"aurora"') is True
        assert await _run(runtime, "test.alpha:get:theme") == "hive"
        assert await _run(runtime, "test.beta:get:theme") == "aurora"
        assert json.loads((tmp_path / "gateway-data" / "store" / "test.alpha.json").read_text()) == {"theme": "hive"}

        runtime.publish(_store_package("test.alpha", tmp_path))  # reload
        assert await _run(runtime, "test.alpha:get:theme") == "hive"
    finally:
        await runtime.shutdown()

    restarted = await _runtime(tmp_path)
    try:
        restarted.publish(_store_package("test.alpha", tmp_path))
        assert await _run(restarted, "test.alpha:get:theme") == "hive"
        assert await _run(restarted, "test.alpha:keys:-") == ["theme"]
        assert await _run(restarted, "test.alpha:delete:theme") is True
        assert await _run(restarted, "test.alpha:get:theme") == "missing"
    finally:
        await restarted.shutdown()


async def test_data_root_and_data_assets(tmp_path):
    results = {}
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    async def ready(event, ops, next_handler):
        root = await ops.data.root()
        results["root"] = root
        media = root / "public" / "themes" / "hive" / "1.0.0"
        media.mkdir(parents=True)
        (media / "poster.bin").write_bytes(b"poster-bytes")
        (root / "private.json").write_text("{}")
        (root / "public" / "escape.bin").symlink_to(outside)
        results["url"] = await ops.ui.asset_url("themes/hive/1.0.0/poster.bin", source="data")
        for bad, source in (("../private.json", "data"), ("escape.bin", "data"),
                            ("themes/hive/1.0.0/poster.bin", "package"), ("x", "elsewhere")):
            with pytest.raises(ValueError):
                await ops.ui.asset_url(bad, source=source)
        return UiActionOutput(accepted=True, status="ready")

    runtime = await _runtime(tmp_path)
    try:
        package_root = tmp_path / "pkg"
        (package_root / "assets").mkdir(parents=True)
        runtime.publish(LoadedPackage(
            package_id="test.surface", version="1.0.0", root=package_root, ui=UI,
            handlers=(HandlerDeclaration("ui.ready", ready, {"target.package_id": "test.surface"}, "ready"),),
        ))
        params = {"event": "ui.ready", "target": {
            "scope": "application", "slot_id": "application.sidebar.footer", "instance_key": "surface",
        }, "payload": {}}
        await runtime.intercept(SimpleNamespace(closed=False), "ready", "webui.dispatch", params)

        assert results["root"] == tmp_path / "gateway-data" / "packages" / "test.surface"
        generation = runtime.browser.generations["test.surface"]
        assert results["url"] == f"/extension-assets/test.surface/{generation}.data/themes/hive/1.0.0/poster.bin"

        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            data = await client.get(results["url"])
            private = await client.get(f"/extension-assets/test.surface/{generation}.data/..%2Fprivate.json")
            as_package = await client.get(f"/extension-assets/test.surface/{generation}/themes/hive/1.0.0/poster.bin")
        assert data.status_code == 200
        assert data.content == b"poster-bytes"
        assert data.headers["content-security-policy"] == "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        assert private.status_code == 404
        assert as_package.status_code == 404
    finally:
        await runtime.shutdown()


async def test_symlinked_package_data_directory_is_refused(tmp_path):
    runtime = await _runtime(tmp_path)
    try:
        target = tmp_path / "elsewhere"
        target.mkdir()
        packages = tmp_path / "gateway-data" / "packages"
        packages.mkdir(parents=True)
        (packages / "test.linked").symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError):
            runtime.package_data_dir("test.linked")
        with pytest.raises(ValueError):
            runtime.package_data_dir("../escape")
    finally:
        await runtime.shutdown()


PROBE_ENTRY = """\
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput


class Extension:
    def register(self, registrar):
        async def probe(event, ops, next_handler):
            if event.event_input.method != "probe":
                return await next_handler(event)
            await ops.store.set("seen", True)
            root = await ops.data.root()
            (root / "note.txt").write_text("kept")
            return GatewayRequestOutput(handled=True, route="consume", response={"result": True})

        registrar.register_event_handler("gateway.request", probe, {}, "user")


def create_extension():
    return Extension()
"""


def _probe_zip(tmp_path: Path, version: str = "0.1.0") -> Path:
    source = tmp_path / "src" / version
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1, "package_type": "extension",
        "package": {"id": "demo.data", "version": version, "domain": "gateway"},
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }), encoding="utf-8")
    (source / "extension.py").write_text(PROBE_ENTRY, encoding="utf-8")
    archive = tmp_path / f"demo.data-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in source.iterdir():
            bundle.write(file, file.name)
    return archive


async def test_reload_keeps_and_uninstall_removes_package_data(tmp_path):
    runtime = await _runtime(tmp_path)
    try:
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        assert [report.status for report in await manager.install([_probe_zip(tmp_path)])] == ["SUCCESS"]
        assert await _run(runtime, "probe") is True
        store_file = tmp_path / "gateway-data" / "store" / "demo.data.json"
        data_dir = tmp_path / "gateway-data" / "packages" / "demo.data"
        assert store_file.is_file() and (data_dir / "note.txt").is_file()

        assert (await manager.reload("demo.data")).status == "SUCCESS"
        assert store_file.is_file() and (data_dir / "note.txt").is_file()

        assert (await manager.uninstall("demo.data")).status == "SUCCESS"
        assert not store_file.exists()
        assert not data_dir.exists()
    finally:
        await runtime.shutdown()


async def test_failed_data_cleanup_keeps_the_installed_copy_for_retry(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    try:
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        await manager.install([_probe_zip(tmp_path)])
        await _run(runtime, "probe")

        original = runtime.cleanup_package_data

        async def failing(package_id):
            raise OSError("disk busy")

        monkeypatch.setattr(runtime, "cleanup_package_data", failing)
        report = await manager.uninstall("demo.data")
        assert report.status != "SUCCESS"
        assert manager.gateway_installed("demo.data")

        monkeypatch.setattr(runtime, "cleanup_package_data", original)
        assert (await manager.uninstall("demo.data")).status == "SUCCESS"
        assert not manager.gateway_installed("demo.data")
        assert not (tmp_path / "gateway-data" / "packages" / "demo.data").exists()
    finally:
        await runtime.shutdown()


def test_data_dir_is_configurable_and_defaults_under_the_workspace(tmp_path, monkeypatch):
    custom = load_gateway_config({"extensions": {"gateway": {"data_dir": str(tmp_path / "custom")}}})
    assert custom.data_dir == (tmp_path / "custom").resolve()

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path / "workspace")
    default = load_gateway_config({})
    assert default.data_dir == (tmp_path / "workspace" / "extension" / "gateway").resolve()
    assert not os.path.exists(default.data_dir)


def _gated_package(package_id: str, root: Path, gate: asyncio.Event, entered: asyncio.Event,
                   captured: dict) -> LoadedPackage:
    """``slow`` waits on ``gate`` before writing; ``keep`` hands its Ops to a later caller."""

    async def command(event, ops, next_handler):
        method = event.event_input.method
        if method == "keep":
            captured["ops"] = ops
        elif method == "slow":
            entered.set()
            await gate.wait()
            await ops.store.set("late", True)
        else:
            return await next_handler(event)
        return GatewayRequestOutput(handled=True, route="consume", response={"result": True})

    return LoadedPackage(package_id=package_id, version="1.0.0", root=root,
                         handlers=(HandlerDeclaration("gateway.request", command, {}, "command"),))


async def test_uninstall_is_busy_while_a_handler_of_the_package_runs(tmp_path):
    gate, entered = asyncio.Event(), asyncio.Event()
    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(_gated_package("test.slow", tmp_path, gate, entered, {}))
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        call = asyncio.create_task(_run(runtime, "slow"))
        await entered.wait()

        report = await manager.uninstall("test.slow")
        assert report.status == "FAILED"
        assert report.error.status is StatusCode.EXTENSION_PACKAGE_BUSY
        assert runtime.is_active_package("test.slow")

        gate.set()
        assert await call is True
        store_file = tmp_path / "gateway-data" / "store" / "test.slow.json"
        assert store_file.is_file()
        assert (await manager.uninstall("test.slow")).status == "SUCCESS"
        assert not store_file.exists()
    finally:
        gate.set()
        await runtime.shutdown()


async def test_ops_of_a_finished_handler_cannot_write_data_after_uninstall(tmp_path):
    captured: dict = {}
    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(_gated_package("test.keep", tmp_path, asyncio.Event(), asyncio.Event(), captured))
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        assert await _run(runtime, "keep") is True
        assert (await manager.uninstall("test.keep")).status == "SUCCESS"

        stale = captured["ops"]
        with pytest.raises(Exception):
            await stale.store.set("late", True)
        with pytest.raises(Exception):
            await stale.data.root()
        assert not (tmp_path / "gateway-data" / "store" / "test.keep.json").exists()
        assert not (tmp_path / "gateway-data" / "packages" / "test.keep").exists()
    finally:
        await runtime.shutdown()


async def test_detached_ops_work_until_their_activation_is_replaced(tmp_path):
    captured: dict = {}
    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(_gated_package("test.keep", tmp_path, asyncio.Event(), asyncio.Event(), captured))
        assert await _run(runtime, "keep") is True
        detached = captured["ops"]
        # A background task of the current activation may still use its Ops.
        assert await detached.store.set("theme", "hive") is True
        assert (await detached.data.root()).is_dir()

        runtime.publish(_gated_package("test.keep", tmp_path, asyncio.Event(), asyncio.Event(), captured))
        with pytest.raises(Exception):
            await detached.store.set("theme", "aurora")
        assert await _run(runtime, "keep") is True
        assert await captured["ops"].store.get("theme") == "hive"
    finally:
        await runtime.shutdown()


async def test_uninstall_is_busy_while_a_detached_data_call_runs(tmp_path):
    captured: dict = {}
    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(_gated_package("test.keep", tmp_path, asyncio.Event(), asyncio.Event(), captured))
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        assert await _run(runtime, "keep") is True
        with runtime.package_data_call("test.keep", captured["ops"]):
            report = await manager.uninstall("test.keep")
            assert report.error.status is StatusCode.EXTENSION_PACKAGE_BUSY
        assert (await manager.uninstall("test.keep")).status == "SUCCESS"
    finally:
        await runtime.shutdown()


async def test_cancelled_data_root_keeps_uninstall_busy_until_the_worker_finishes(tmp_path, monkeypatch):
    captured: dict = {}
    runtime = await _runtime(tmp_path)
    package_id = "test.cancel-root"
    started, finished = asyncio.Event(), asyncio.Event()
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    mkdir = Path.mkdir
    data_dir = runtime.package_data_dir(package_id)
    call = None

    def slow_mkdir(path, *args, **kwargs):
        if path != data_dir:
            return mkdir(path, *args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        try:
            if not resume.wait(5):
                raise TimeoutError("data-root test worker was not released")
            return mkdir(path, *args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    try:
        runtime.publish(_gated_package(package_id, tmp_path, asyncio.Event(), asyncio.Event(), captured))
        assert await _run(runtime, "keep") is True
        monkeypatch.setattr(Path, "mkdir", slow_mkdir)
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        call = asyncio.create_task(captured["ops"].data.root())
        await asyncio.wait_for(started.wait(), 2)
        call.cancel()
        await asyncio.sleep(0)

        report = await manager.uninstall(package_id)
        assert report.status == "FAILED", "a cancelled await must still count its live file worker"
        assert report.error.status is StatusCode.EXTENSION_PACKAGE_BUSY
        assert runtime.is_active_package(package_id)

        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(finished.wait(), 2)
        assert data_dir.is_dir()
        assert (await manager.uninstall(package_id)).status == "SUCCESS"
        assert not data_dir.exists()
    finally:
        resume.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        if started.is_set():
            await asyncio.wait_for(finished.wait(), 2)
        await runtime.shutdown()


async def test_a_chain_collected_before_uninstall_skips_the_released_handler(tmp_path):
    gate, entered = asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def first(event, ops, next_handler):
        if event.event_input.method == "chain":
            entered.set()
            await gate.wait()
        return await next_handler(event)

    async def second(event, ops, next_handler):
        if event.event_input.method != "chain":
            return await next_handler(event)
        ran.append("second")
        await ops.store.set("late", True)
        return GatewayRequestOutput(handled=True, route="consume", response={"result": "second"})

    runtime = await _runtime(tmp_path)
    try:
        runtime.publish(LoadedPackage(package_id="test.first", version="1.0.0", root=tmp_path,
                                      handlers=(HandlerDeclaration("gateway.request", first, {}, "first"),)))
        runtime.publish(LoadedPackage(package_id="test.second", version="1.0.0", root=tmp_path,
                                      handlers=(HandlerDeclaration("gateway.request", second, {}, "second"),)))
        manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
        call = asyncio.create_task(runtime.intercept(SimpleNamespace(closed=False), "chain", "chain", {}))
        await entered.wait()
        assert (await manager.uninstall("test.second")).status == "SUCCESS"
        gate.set()
        await call
        assert ran == []
        assert not (tmp_path / "gateway-data" / "store" / "test.second.json").exists()
    finally:
        gate.set()
        await runtime.shutdown()
