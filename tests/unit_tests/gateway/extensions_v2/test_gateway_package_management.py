# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway package management over the shared ``extension_pkgs`` store."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ExtensionFailure

from jiuwenswarm.gateway.channel_manager.web import extension_package_handlers
from jiuwenswarm.gateway.extensions_v2 import assembly
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.management.catalog import list_installed, package_detail
from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime


class Channel:
    """Minimal channel: methods, responses and connection ids."""

    channel_id = "web"

    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []

    def register_method(self, name: str, handler) -> None:
        self.methods[name] = handler

    async def ensure_ws_id(self, ws) -> str:
        if not getattr(ws, "_jiuwen_ws_id", ""):
            ws._jiuwen_ws_id = "ws-1"
        return ws._jiuwen_ws_id

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


ENTRY = """\
class Extension:
    def register(self, registrar):
        async def handler(event, ops, next_handler):
            return await next_handler(event)

        registrar.register_event_handler("gateway.request", handler, {}, "user")


def create_extension():
    return Extension()
"""

BAD_ENTRY = """\
class Extension:
    def register(self, registrar):
        async def handler(event, ops, next_handler):
            return await next_handler(event)

        registrar.register_event_handler("gateway.request", handler, {}, "user")
        registrar.register_event_handler("not.a.gateway.event", handler, {}, "user")


def create_extension():
    return Extension()
"""


AUTH_ENTRY = """\
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput

class Extension:
    def register(self, registrar):
        async def handler(event, ops, next_handler):
            if event.event_input.headers.get("authorization") != "Bearer valid":
                return GatewayRequestOutput(handled=True, route="deny", error={"code": "AUTH_REQUIRED"})
            return await next_handler(event)

        registrar.register_event_handler("gateway.request", handler)

def create_extension():
    return Extension()
"""


def _package_zip(
    tmp_path: Path,
    *,
    package_id: str = "demo.gateway",
    domain: str = "gateway",
    version: str = "0.1.0",
    entry: str = ENTRY,
    enable: bool | None = None,
) -> Path:
    source = tmp_path / "src" / package_id / version
    source.mkdir(parents=True)
    manifest_document = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": package_id, "version": version, "domain": domain},
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }
    if enable is not None:
        manifest_document["enable"] = enable
    manifest = json.dumps(manifest_document)
    (source / "manifest.json").write_text(manifest, encoding="utf-8")
    (source / "extension.py").write_text(entry, encoding="utf-8")
    archive = tmp_path / f"{package_id}-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in source.iterdir():
            bundle.write(file, file.name)
    return archive


def _package_dir(tmp_path: Path, **manifest_extra) -> Path:
    root = tmp_path / "examples" / "demo.gateway"
    root.mkdir(parents=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "expose": True,
        "package": {"id": "demo.gateway", "version": "0.1.0", "domain": "gateway"},
        **manifest_extra,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_detail_projects_preview_readme_and_events(tmp_path):
    root = _package_dir(
        tmp_path,
        display={
            "details": "## 能力\n- 示例能力",
            "events": [{"name": "gateway.request", "description": {"zh": "放行请求", "en": "Route requests"}}],
        },
    )
    (root / "README.md").write_text("# 生效机制\n读取请求头", encoding="utf-8")
    image = b"\x89PNG-preview"
    (root / "assets").mkdir()
    (root / "assets" / "preview.png").write_bytes(image)

    detail = package_detail("demo.gateway", tmp_path / "examples", {})

    assert detail["details"] == {"zh": "## 能力\n- 示例能力", "en": "## 能力\n- 示例能力"}
    assert detail["readme"] == {"zh": "# 生效机制\n读取请求头", "en": "# 生效机制\n读取请求头"}
    assert detail["preview"] == f"data:image/png;base64,{base64.b64encode(image).decode()}"
    assert detail["events"] == [
        {"name": "gateway.request", "description": {"zh": "放行请求", "en": "Route requests"}}
    ]


def test_detail_omits_missing_preview_and_readme(tmp_path):
    _package_dir(tmp_path)

    detail = package_detail("demo.gateway", tmp_path / "examples", {})

    assert detail["preview"] is None
    assert detail["readme"] == {"zh": "", "en": ""}
    assert detail["details"] == {"zh": "", "en": ""}
    assert detail["events"] == []


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(
        channel=Channel(),
        config=GatewayExtensionConfig(
            install_dir=tmp_path / "extension_pkgs",
            recovery_data_dir=tmp_path / "extension",
        ),
    )
    await runtime.start()
    return runtime


async def test_gateway_package_install_publish_and_uninstall(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")

    reports = await manager.install([_package_zip(tmp_path)])

    assert [report.status for report in reports] == ["SUCCESS"]
    assert runtime.user_handler_count == 1
    assert [package.package_id for package in runtime.active_packages()] == ["demo.gateway"]
    state = manager.query()
    assert state["installed_packages"][0]["package_id"] == "demo.gateway"
    assert state["installed_packages"][0]["domain"] == "gateway"
    assert state["active"][0]["version"] == "0.1.0"

    report = await manager.uninstall("demo.gateway")

    assert report.status == "SUCCESS"
    assert runtime.user_handler_count == 0
    assert manager.installed() == []


HELPER_ENTRY = """\
from .helpers import ANSWER


class Extension:
    def register(self, registrar):
        async def handler(event, ops, next_handler):
            return await next_handler(event)

        registrar.register_event_handler("gateway.request", handler, {}, "user")


def create_extension():
    return Extension()
"""


def _helper_package_zip(folder: Path, answer: str) -> Path:
    """demo.gateway 0.1.0 whose entry imports a sibling module holding ``answer``."""
    source = folder / "src"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1, "package_type": "extension",
        "package": {"id": "demo.gateway", "version": "0.1.0", "domain": "gateway"},
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }), encoding="utf-8")
    (source / "extension.py").write_text(HELPER_ENTRY, encoding="utf-8")
    (source / "helpers.py").write_text(f"ANSWER = {answer!r}\n", encoding="utf-8")
    archive = folder / "demo.gateway-0.1.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in source.iterdir():
            bundle.write(file, file.name)
    return archive


async def test_reinstalling_the_same_version_loads_fresh_submodules(tmp_path):
    """Each load uses new module names, so a reinstall at the same path never reuses cached sibling modules."""
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    try:
        for answer in ("first", "second"):
            reports = await manager.install([_helper_package_zip(tmp_path / answer, answer)])
            assert [report.status for report in reports] == ["SUCCESS"]
            [package] = runtime.active_packages()
            assert package.handlers[0].handler.__globals__["ANSWER"] == answer
            assert (await manager.uninstall("demo.gateway")).status == "SUCCESS"
    finally:
        await runtime.shutdown()


def _package_modules(package) -> list[str]:
    roots = package.module_names
    return [name for name in sys.modules if any(name == root or name.startswith(root + ".") for root in roots)]


async def test_released_loads_leave_sys_modules(tmp_path):
    """Loads no longer accumulate: each release drops the modules of its own load (S_01 invariant 11)."""
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    loads = []
    try:
        for index in range(6):
            reports = await manager.install([_helper_package_zip(tmp_path / f"load{index}", f"answer{index}")])
            assert [report.status for report in reports] == ["SUCCESS"]
            [package] = runtime.active_packages()
            assert len(_package_modules(package)) >= 2  # entry package and the helpers submodule
            loads.append(package)
            assert (await manager.uninstall("demo.gateway")).status == "SUCCESS"
            assert _package_modules(package) == []
        assert all(_package_modules(package) == [] for package in loads)

        await manager.install([_helper_package_zip(tmp_path / "reload", "kept")])
        assert (await manager.reload("demo.gateway")).status == "SUCCESS"
        [current] = runtime.active_packages()
        assert _package_modules(current) and all(_package_modules(package) == [] for package in loads)
    finally:
        await runtime.shutdown()
    assert _package_modules(current) == []


async def test_modules_stay_until_running_handlers_finish(tmp_path):
    runtime = await _runtime(tmp_path)
    started, resume = asyncio.Event(), asyncio.Event()
    root = tmp_path / "slow"
    root.mkdir()
    module_name = "jiuwenswarm_gateway_ext_test_slow"
    sys.modules[module_name] = types.ModuleType(module_name)

    async def slow(event, ops, next_handler):
        started.set()
        await resume.wait()
        return await next_handler(event)

    package = LoadedPackage(package_id="demo.slow", version="1.0.0", root=root, module_names=(module_name,),
                            handlers=(HandlerDeclaration("gateway.request", slow, {}, "slow"),))
    try:
        runtime.publish(package)
        request = asyncio.create_task(runtime.intercept(SimpleNamespace(closed=False), "req", "ping", {}))
        await started.wait()
        runtime.unpublish("demo.slow")
        assert module_name in sys.modules, "a running handler keeps its modules"
        resume.set()
        await request
        assert module_name not in sys.modules
    finally:
        sys.modules.pop(module_name, None)
        await runtime.shutdown()
async def test_gateway_disabled_package_keeps_the_copy_without_publishing(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")

    reports = await manager.install([_package_zip(tmp_path, enable=False)])

    assert [report.status for report in reports] == ["SUCCESS"]
    assert runtime.user_handler_count == 0
    assert manager.owns("demo.gateway")

    enabled = await manager.set_enabled("demo.gateway", True)

    assert enabled.status == "SUCCESS"
    assert runtime.user_handler_count == 1
    disabled = await manager.set_enabled("demo.gateway", False)
    assert disabled.status == "SUCCESS"
    assert runtime.user_handler_count == 0


async def test_installing_a_disabled_copy_releases_an_active_gateway_package(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path / "enabled")])
    assert runtime.user_handler_count == 1

    reports = await manager.install([_package_zip(tmp_path / "disabled", version="0.2.0", enable=False)])

    assert [report.status for report in reports] == ["SUCCESS"]
    assert runtime.user_handler_count == 0
    manifest = json.loads(
        (tmp_path / "extension_pkgs" / "demo.gateway" / "0.2.0" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["enable"] is False


async def test_reload_of_a_disabled_gateway_package_succeeds_without_enabling_it(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path, enable=False)])

    report = await manager.reload("demo.gateway")

    assert report.status == "SUCCESS"
    assert report.error is None
    assert runtime.user_handler_count == 0
    manifest = json.loads(
        (tmp_path / "extension_pkgs" / "demo.gateway" / "0.1.0" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["enable"] is False


async def test_agent_package_is_rejected_without_copying(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")

    reports = await manager.install([_package_zip(tmp_path, package_id="demo.agent", domain="agentserver")])

    assert [report.status for report in reports] == ["FAILED"]
    assert reports[0].error.code == "EXTENSION_DOMAIN_MISMATCH"
    assert manager.installed() == []
    assert runtime.user_handler_count == 0


async def test_reload_republishes_the_installed_copy(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path)])
    runtime.unpublish("demo.gateway")
    assert runtime.user_handler_count == 0

    report = await manager.reload("demo.gateway")

    assert report.status == "SUCCESS"
    assert runtime.user_handler_count == 1


async def test_query_frame_is_answered_locally_for_gateway_packages(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    await GatewayPackageManager(runtime, tmp_path / "extension_pkgs").install([_package_zip(tmp_path)])
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    channel = runtime.channel
    ws = SimpleNamespace()

    handled = await extension_package_handlers._handle_locally(
        channel=channel,
        resolve_client=lambda: None,
        ws=ws,
        req_id="r1",
        params={"package_id": "demo.gateway"},
        session_id=None,
        user_id=None,
        method=extension_package_handlers.ReqMethod.EXTENSIONS_V2_QUERY,
    )

    assert handled is True
    assert channel.responses[0]["payload"]["state"]["installed_packages"][0]["package_id"] == "demo.gateway"


async def test_uninstall_of_an_agent_package_is_forwarded(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)

    handled = await extension_package_handlers._handle_locally(
        channel=runtime.channel,
        resolve_client=lambda: None,
        ws=SimpleNamespace(),
        req_id="r2",
        params={"package_id": "demo.agent"},
        session_id=None,
        user_id=None,
        method=extension_package_handlers.ReqMethod.EXTENSIONS_V2_UNINSTALL,
    )

    assert handled is False
    assert runtime.channel.responses == []


async def test_management_frames_follow_user_request_policy(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    await GatewayPackageManager(runtime, tmp_path / "extension_pkgs").install(
        [_package_zip(tmp_path, entry=AUTH_ENTRY)]
    )
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    channel = runtime.channel
    ws = SimpleNamespace()
    method = extension_package_handlers.ReqMethod.EXTENSIONS_V2_UNINSTALL.value

    visited, _ = await runtime.intercept(ws, "r3", method, {"package_id": "demo.gateway"})

    assert visited is True
    assert channel.responses[0]["code"] == "AUTH_REQUIRED"

    visited, _ = await runtime.intercept(
        ws, "r3b", method, {"package_id": "demo.gateway"}, {"authorization": "Bearer valid"}
    )

    assert visited is False

    handled = await extension_package_handlers._handle_locally(
        channel=channel,
        resolve_client=lambda: None,
        ws=ws,
        req_id="r4",
        params={"package_id": "demo.gateway"},
        session_id=None,
        user_id=None,
        method=extension_package_handlers.ReqMethod.EXTENSIONS_V2_UNINSTALL,
    )

    assert handled is True
    assert channel.responses[1]["ok"] is True


async def test_partial_registration_failure_rolls_the_package_back(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")

    reports = await manager.install([_package_zip(tmp_path, entry=BAD_ENTRY)])

    assert [report.status for report in reports] == ["FAILED"]
    assert reports[0].error.code == "EXTENSION_LOAD_FAILED"
    assert runtime.user_handler_count == 0
    assert runtime.active_packages() == ()
    assert manager.installed() == []


async def test_uninstall_reports_failure_when_the_copy_cannot_be_deleted(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path)])

    def deny_delete(package_id: str, version: str) -> None:
        raise ExtensionFailure(
            StatusCode.EXTENSION_INSTALL_COPY_FAILED,
            reason="file is locked",
        )

    monkeypatch.setattr(manager._store, "remove_installed", deny_delete)

    report = await manager.uninstall("demo.gateway")

    assert report.status == "FAILED"
    assert report.error.code == "EXTENSION_INSTALL_COPY_FAILED"
    assert runtime.user_handler_count == 0
    assert manager.installed() != []


async def test_local_cli_uses_live_gateway_manager(tmp_path, monkeypatch):
    from jiuwenswarm.channels.cli import extension as cli
    from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    archive = _package_zip(tmp_path)
    try:
        result, code = await cli.execute(cli.parser().parse_args(["install", str(archive), "--json"]))
        assert code == 0 and result["domain"] == "gateway" and result["active"]
        assert runtime.user_handler_count == 1
        result, code = await cli.execute(cli.parser().parse_args(["reload", "demo.gateway", "--domain", "gateway"]))
        assert code == 0 and result["verification_status"] == "SUCCESS"
        result, code = await cli.execute(cli.parser().parse_args(["uninstall", "demo.gateway", "--domain", "gateway"]))
        assert code == 0 and not result["active"] and runtime.user_handler_count == 0
    finally:
        await service.close()
        await runtime.shutdown()


async def test_local_cli_reload_asks_mounted_slots_for_ready(tmp_path, monkeypatch):
    """Reload retires the old activation's views, so browsers must ask the new one to render (F_11)."""
    from jiuwenswarm.channels.cli import extension as cli
    from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    runtime = await _runtime(tmp_path)
    broadcasts = []

    async def broadcast_event(event, payload, **_):
        broadcasts.append((event, payload))

    runtime.channel.broadcast_event = broadcast_event
    service = await start_local_cli(runtime)
    archive = _package_zip(tmp_path)
    try:
        await cli.execute(cli.parser().parse_args(["install", str(archive), "--json"]))
        result, code = await cli.execute(cli.parser().parse_args(["reload", "demo.gateway", "--domain", "gateway"]))
        assert code == 0 and result["verification_status"] == "SUCCESS"
        assert broadcasts == [("extensions.v2.installed", {"package_ids": ["demo.gateway"]})] * 2
        await cli.execute(cli.parser().parse_args(["uninstall", "demo.gateway", "--domain", "gateway"]))
        assert len(broadcasts) == 2
    finally:
        await service.close()
        await runtime.shutdown()


async def test_local_cli_retains_failed_gateway_report(tmp_path, monkeypatch):
    from jiuwenswarm.channels.cli import extension as cli
    from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    archive = _package_zip(tmp_path, entry=BAD_ENTRY)
    try:
        result, code = await cli.execute(cli.parser().parse_args(["install", str(archive)]))
        assert code == 1 and result["operation_status"] == "FAILED"
        assert isinstance(result["reports"][0]["error"]["code"], str)
        assert not runtime.active_packages()
    finally:
        await service.close()
        await runtime.shutdown()


async def test_gateway_assembly_publishes_and_revokes_cli(tmp_path, monkeypatch):
    from jiuwenswarm.channels.cli import extension as cli

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    first = await assembly.install_gateway_extensions(
        Channel(), GatewayExtensionConfig(install_dir=tmp_path / "extension_pkgs"),
    )
    try:
        assert first is not None
        result, code = await cli.execute(cli.parser().parse_args(["query", "--domain", "gateway"]))
        assert code == 0 and result["state"]["active"] == []
    finally:
        await assembly.shutdown_gateway_extensions()
    result, code = await cli.execute(cli.parser().parse_args(["query", "--domain", "gateway"]))
    assert code == 2 and result["error"]["code"] == "HOST_UNAVAILABLE"


@pytest.mark.parametrize("package_id", [None, "", False])
async def test_local_cli_rejects_invalid_explicit_query_id(tmp_path, monkeypatch, package_id):
    from jiuwenswarm.common import extension_cli
    from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli

    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    try:
        response = await extension_cli.request(
            extension_cli.read_context("gateway"), "query", {"package_id": package_id}, 2,
        )
        assert not response["ok"]
        assert response["payload"]["code"] == "INVALID_PARAMS"
    finally:
        await service.close()
        await runtime.shutdown()


async def test_uninstall_retries_a_transient_delete_lock(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path)])
    original = manager._store.remove_installed
    calls = {"count": 0}

    def flaky(package_id: str, version: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ExtensionFailure(StatusCode.EXTENSION_INSTALL_COPY_FAILED, reason="file is locked")
        original(package_id, version)

    monkeypatch.setattr(manager._store, "remove_installed", flaky)

    report = await manager.uninstall("demo.gateway")

    assert report.status == "SUCCESS"
    assert calls["count"] == 2
    assert manager.installed() == []


def _example_package(examples_dir: Path, package_id: str, domain: str) -> Path:
    root = examples_dir / package_id
    root.mkdir(parents=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": package_id, "version": "0.1.0", "displayName": package_id, "domain": domain},
        "expose": True,
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "extension.py").write_text(ENTRY, encoding="utf-8")
    return root


def _local_gateway(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    install_dir = tmp_path / "extension_pkgs"
    monkeypatch.setattr(extension_package_handlers, "resolve_examples_dir", lambda: examples_dir)
    return examples_dir, install_dir


async def _handle(runtime: GatewayRuntime, method, params, req_id="r") -> bool:
    return await extension_package_handlers._handle_locally(
        channel=runtime.channel,
        resolve_client=lambda: None,
        ws=SimpleNamespace(),
        req_id=req_id,
        params=params,
        session_id=None,
        user_id=None,
        method=method,
    )


async def test_catalog_lists_gateway_presets_before_any_install(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    _example_package(examples_dir, "demo.gateway", "gateway")
    _example_package(examples_dir, "demo.agent", "agentserver")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_CATALOG, {"scope": "catalog"}
    )

    assert handled is True
    payload = runtime.channel.responses[0]["payload"]
    assert [package["id"] for package in payload["packages"]] == ["demo.gateway"]


def test_catalog_skips_an_active_record_without_a_string_root(tmp_path):
    packages = list_installed(tmp_path, {"active": [{"package_id": "demo.gateway", "root": None}]})

    assert packages == []


async def test_catalog_lists_disabled_installed_gateway_package(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    await runtime.packages.install([_package_zip(tmp_path, enable=False)])

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_CATALOG, {"scope": "mine"}
    )

    assert handled is True
    assert runtime.channel.responses[0]["payload"]["packages"] == [
        {
            "id": "demo.gateway",
            "version": "0.1.0",
            "displayName": {"zh": "demo.gateway", "en": "demo.gateway"},
            "displayDescription": {"zh": "", "en": ""},
            "tags": [],
            "avatar": None,
            "installed": True,
            "active": False,
            "enabled": False,
        }
    ]


async def test_set_enabled_accepts_proxy_metadata_fields(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    await runtime.packages.install([_package_zip(tmp_path, enable=False)])

    handled = await _handle(
        runtime,
        extension_package_handlers.ReqMethod.EXTENSIONS_V2_SET_ENABLED,
        {"package_id": "demo.gateway", "enable": True, "session_id": "session-1"},
    )

    assert handled is True
    assert runtime.channel.responses[0]["ok"] is True
    assert runtime.user_handler_count == 1


async def test_show_answers_for_an_uninstalled_gateway_preset(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    _example_package(examples_dir, "demo.gateway", "gateway")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_SHOW, {"package_id": "demo.gateway"}
    )

    assert handled is True
    assert runtime.channel.responses[0]["payload"]["package"]["id"] == "demo.gateway"


async def test_show_of_an_agent_package_is_forwarded(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    _example_package(examples_dir, "demo.agent", "agentserver")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_SHOW, {"package_id": "demo.agent"}
    )

    assert handled is False
    assert runtime.channel.responses == []


async def test_install_by_package_id_is_answered_locally_for_gateway(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, install_dir = _local_gateway(tmp_path, monkeypatch)
    _example_package(examples_dir, "demo.gateway", "gateway")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_INSTALL, {"package_ids": ["demo.gateway"]}
    )

    assert handled is True
    assert runtime.channel.responses[0]["payload"]["reports"][0]["status"] == "SUCCESS"
    assert (install_dir / "demo.gateway" / "0.1.0" / "manifest.json").is_file()
    assert [package.package_id for package in runtime.active_packages()] == ["demo.gateway"]


async def test_install_by_package_id_is_forwarded_for_agent(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, install_dir = _local_gateway(tmp_path, monkeypatch)
    _example_package(examples_dir, "demo.agent", "agentserver")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_INSTALL, {"package_ids": ["demo.agent"]}
    )

    assert handled is False
    assert runtime.channel.responses == []
    assert not (install_dir / "demo.agent").exists()


def _plugin_package(root: Path) -> Path:
    root.mkdir(parents=True)
    manifest = {"package_type": "plugin", "schemaVersion": 1, "name": "demo-plugin", "version": "1.0.0"}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


async def test_import_local_installs_a_gateway_package(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, install_dir = _local_gateway(tmp_path, monkeypatch)
    source = _example_package(examples_dir, "demo.gateway", "gateway")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(source)}
    )

    assert handled is True
    payload = runtime.channel.responses[0]["payload"]
    assert payload["kind"] == "extension"
    assert [report["status"] for report in payload["reports"]] == ["SUCCESS"]
    assert (install_dir / "demo.gateway" / "0.1.0" / "manifest.json").is_file()


async def test_import_local_routes_agent_packages_to_the_agent_server(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    calls: list[dict] = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return True, {"reports": []}

    monkeypatch.setattr(extension_package_handlers, "fetch_agent_unary", fake_fetch)

    extension_source = _example_package(examples_dir, "demo.agent", "agentserver")
    await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(extension_source)}
    )
    plugin_source = _plugin_package(tmp_path / "plugin-src")
    await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(plugin_source)}
    )

    assert [call["req_method"] for call in calls] == [
        extension_package_handlers.ReqMethod.EXTENSIONS_V2_INSTALL,
        extension_package_handlers.ReqMethod.PLUGIN_PACKAGES_IMPORT_LOCAL,
    ]
    assert calls[0]["params"] == {"paths": [str(extension_source)]}
    assert calls[1]["params"] == {"path": str(plugin_source)}


async def test_import_local_routes_a_zipped_plugin_package(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    _local_gateway(tmp_path, monkeypatch)
    calls: list[dict] = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return True, {"id": "demo-plugin"}

    monkeypatch.setattr(extension_package_handlers, "fetch_agent_unary", fake_fetch)

    source = tmp_path / "plugin-src"
    source.mkdir()
    manifest = {"package_type": "plugin", "schemaVersion": 1, "name": "demo-plugin", "version": "1.0.0"}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "demo-plugin.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(source / "manifest.json", "manifest.json")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(archive)}
    )

    assert handled is True
    assert calls[0]["req_method"] == extension_package_handlers.ReqMethod.PLUGIN_PACKAGES_IMPORT_LOCAL
    assert calls[0]["params"] == {"path": str(archive)}


async def test_import_local_rejects_an_unknown_package_type(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    _local_gateway(tmp_path, monkeypatch)
    source = tmp_path / "unknown-src"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"package_type": "mcp"}), encoding="utf-8")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(source)}
    )

    assert handled is True
    assert runtime.channel.responses[0]["ok"] is False
    assert runtime.channel.responses[0]["code"] == extension_package_handlers.INVALID_PARAMS_CODE


async def test_import_local_rejects_the_legacy_agent_domain(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    source = _example_package(examples_dir, "demo.legacy", "agent")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(source)}
    )

    assert handled is True
    assert runtime.channel.responses[0]["ok"] is False
    assert runtime.channel.responses[0]["code"] == extension_package_handlers.INVALID_PARAMS_CODE
