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
    requires: dict | None = None,
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
    if requires is not None:
        manifest_document["requires"] = requires
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


async def test_gateway_install_switches_to_a_new_version_and_rejects_the_same_one(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path)])

    upgraded = await manager.install([_package_zip(tmp_path, version="0.2.0")])

    assert [report.status for report in upgraded] == ["SUCCESS"]
    assert (tmp_path / "extension_pkgs" / "demo.gateway" / "0.1.0").is_dir()
    assert (tmp_path / "extension_pkgs" / "demo.gateway" / "0.2.0").is_dir()
    assert runtime.active_record("demo.gateway").version == "0.2.0"

    duplicate = await manager.install([_package_zip(tmp_path / "again", version="0.2.0")])

    assert duplicate[0].status == "FAILED"
    assert duplicate[0].error.code == "EXTENSION_PACKAGE_EXISTS"


async def test_gateway_uninstall_one_version_keeps_the_other(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    await manager.install([_package_zip(tmp_path)])
    await manager.install([_package_zip(tmp_path, version="0.2.0")])

    report = await manager.uninstall("demo.gateway", "0.2.0")

    assert report.status == "SUCCESS"
    assert (tmp_path / "extension_pkgs" / "demo.gateway" / "0.1.0").is_dir()
    assert not (tmp_path / "extension_pkgs" / "demo.gateway" / "0.2.0").exists()
    assert runtime.active_record("demo.gateway") is None


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
            "packageId": "demo.gateway",
            "assetId": "",
            "version": "0.1.0",
            "installedVersion": "0.1.0",
            "versions": ["0.1.0"],
            "displayName": {"zh": "demo.gateway", "en": "demo.gateway"},
            "displayDescription": {"zh": "", "en": ""},
            "tags": [],
            "avatar": None,
            "source": "local",
            "origin": "user",
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


async def test_set_enabled_notifies_mounted_slots_after_enabling(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    await runtime.packages.install([_package_zip(tmp_path, enable=False)])
    notices = []

    async def broadcast(event, payload):
        notices.append((event, payload))

    runtime.channel.broadcast_event = broadcast

    await _handle(
        runtime,
        extension_package_handlers.ReqMethod.EXTENSIONS_V2_SET_ENABLED,
        {"package_id": "demo.gateway", "enable": True},
    )

    assert runtime.channel.responses[0]["payload"]["ui_notified"] is True
    assert notices == [("extensions.v2.installed", {"package_ids": ["demo.gateway"]})]

    await _handle(
        runtime,
        extension_package_handlers.ReqMethod.EXTENSIONS_V2_SET_ENABLED,
        {"package_id": "demo.gateway", "enable": False},
        req_id="r2",
    )

    assert runtime.channel.responses[1]["payload"]["reports"][0]["operation"] == "disable"
    assert notices == [("extensions.v2.installed", {"package_ids": ["demo.gateway"]})]


async def test_set_enabled_again_succeeds_without_activating_twice(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    await runtime.packages.install([_package_zip(tmp_path, enable=True)])

    handled = await _handle(
        runtime,
        extension_package_handlers.ReqMethod.EXTENSIONS_V2_SET_ENABLED,
        {"package_id": "demo.gateway", "enable": True},
    )

    assert handled is True
    assert runtime.channel.responses[0]["ok"] is True


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


@pytest.mark.parametrize("action", ["import", "install", "reload"])
async def test_web_management_notifies_mounted_slots_after_release_settles(tmp_path, monkeypatch, action):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    source = _example_package(examples_dir, "demo.gateway", "gateway")
    if action == "reload":
        assert (await runtime.packages.install([source]))[0].status == "SUCCESS"
    order = []
    settle = runtime.browser.settle

    async def settled():
        await settle()
        order.append("released")

    async def broadcast(event, payload):
        order.append((event, payload))

    monkeypatch.setattr(runtime.browser, "settle", settled)
    runtime.channel.broadcast_event = broadcast
    method = getattr(extension_package_handlers.ReqMethod, "EXTENSIONS_V2_" + action.upper())
    params = {"path": str(source)} if action == "import" else (
        {"package_ids": ["demo.gateway"]} if action == "install" else {"package_id": "demo.gateway"}
    )
    try:
        assert await _handle(runtime, method, params)
        assert order == ["released", ("extensions.v2.installed", {"package_ids": ["demo.gateway"]})]
        assert runtime.channel.responses[-1]["payload"]["ui_notified"] is True
    finally:
        await runtime.shutdown()


async def test_web_notification_failure_preserves_the_confirmed_install_result(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    examples_dir, _ = _local_gateway(tmp_path, monkeypatch)
    source = _example_package(examples_dir, "demo.gateway", "gateway")

    async def broadcast(event, payload):
        raise ConnectionError("browser closed")

    runtime.channel.broadcast_event = broadcast
    try:
        assert await _handle(runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(source)})
        payload = runtime.channel.responses[-1]["payload"]
        assert payload["reports"][0]["status"] == "SUCCESS"
        assert payload["ui_notified"] is False
        assert runtime.is_active_package("demo.gateway")
    finally:
        await runtime.shutdown()


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


@pytest.mark.asyncio
async def test_import_local_routes_a_managed_agent_package_zip_to_the_agent_server(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    _local_gateway(tmp_path, monkeypatch)
    calls: list[dict] = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return True, {"reports": []}

    monkeypatch.setattr(extension_package_handlers, "fetch_agent_unary", fake_fetch)
    source = _example_package(tmp_path / "source", "demo.agent", " AgentServer ")
    archive = tmp_path / "demo-agent.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for entry in source.rglob("*"):
            if entry.is_file():
                bundle.write(entry, f"demo.agent/0.1.0/{entry.relative_to(source).as_posix()}")

    handled = await _handle(
        runtime, extension_package_handlers.ReqMethod.EXTENSIONS_V2_IMPORT, {"path": str(archive)}
    )

    assert handled is True
    assert calls[0]["req_method"] == extension_package_handlers.ReqMethod.EXTENSIONS_V2_INSTALL
    assert calls[0]["params"] == {"paths": [str(archive)]}


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


# ── shared install report (production path) ──────────────────────────────────


def _report_path(tmp_path: Path) -> Path:
    return tmp_path / "extension" / "extension_install_report.yaml"


async def _reported_manager(tmp_path: Path, runtime: GatewayRuntime | None = None) -> GatewayPackageManager:
    runtime = runtime or await _runtime(tmp_path)
    manager = GatewayPackageManager(
        runtime,
        tmp_path / "extension_pkgs",
        report_path=_report_path(tmp_path),
        poll_interval=0.05,
        dependency_timeout=5.0,
        lock_timeout=2.0,
    )
    await manager.init()
    return manager


async def test_reported_disable_and_enable_keep_copy_and_settle_report(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        assert (await manager.install([_package_zip(tmp_path)]))[0].status == "SUCCESS"
        root = manager.gateway_installed("demo.gateway")[0].root
        for enabled in (False, True):
            report = await manager.set_enabled("demo.gateway", enabled)
            assert report.status == "SUCCESS"
            assert report.operation == ("enable" if enabled else "disable")
            assert runtime.user_handler_count == int(enabled)
            assert root.exists()
            assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["enable"] is enabled
            state = (await manager._report.read_packages("demo.gateway")).get("demo.gateway")
            if enabled:
                assert state is not None
                assert state.pending is None
                assert state.active_version == "0.1.0"
            else:
                assert state is None
    finally:
        await manager.close()
        await runtime.shutdown()


async def test_reported_install_switches_to_a_new_version_and_rejects_the_same_one(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        assert (await manager.install([_package_zip(tmp_path)]))[0].status == "SUCCESS"

        upgraded = await manager.install([_package_zip(tmp_path, version="0.2.0")])

        assert [report.status for report in upgraded] == ["SUCCESS"]
        assert (tmp_path / "extension_pkgs" / "demo.gateway" / "0.1.0").is_dir()
        assert runtime.active_record("demo.gateway").version == "0.2.0"

        duplicate = await manager.install([_package_zip(tmp_path / "again", version="0.2.0")])

        assert duplicate[0].status == "FAILED"
        assert duplicate[0].error.code == "EXTENSION_PACKAGE_EXISTS"
    finally:
        await manager.close()
        await runtime.shutdown()


def test_merge_packages_applies_gateway_state_to_remote_cards():
    remote = [
        {
            "id": "asset-1",
            "packageId": "webui.login-demo",
            "installed": False,
            "active": False,
            "enabled": True,
        }
    ]
    state = {
        "active": [{"package_id": "webui.login-demo"}],
        "installed_packages": [{"package_id": "webui.login-demo", "version": "1.0.0"}],
    }

    merged = extension_package_handlers._merge_packages([], remote, state)

    assert merged[0]["installed"] is True
    assert merged[0]["active"] is True
    assert merged[0]["enabled"] is True


async def test_reported_uninstall_one_version_keeps_the_other(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        await manager.install([_package_zip(tmp_path, version="0.2.0")])

        report = await manager.uninstall("demo.gateway", "0.2.0")

        assert report.status == "SUCCESS"
        assert (tmp_path / "extension_pkgs" / "demo.gateway" / "0.1.0").is_dir()
        assert not (tmp_path / "extension_pkgs" / "demo.gateway" / "0.2.0").exists()
        assert runtime.active_record("demo.gateway") is None
    finally:
        await manager.close()
        await runtime.shutdown()


async def test_reported_disable_rejects_active_dependents_without_side_effects(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    released = []
    manager._on_released = released.append
    try:
        sources = [
            _package_zip(tmp_path),
            _package_zip(tmp_path, package_id="demo.consumer", requires={
                "packages": [{"id": "demo.gateway", "version": ">=0.1.0"}],
            }),
        ]
        assert all(report.status == "SUCCESS" for report in await manager.install(sources))
        before = runtime.active_record("demo.gateway")
        root = manager.gateway_installed("demo.gateway")[0].root
        original = (root / "manifest.json").read_bytes()

        report = await manager.set_enabled("demo.gateway", False)

        assert report.status == "FAILED"
        assert report.error.code == "EXTENSION_PACKAGE_IN_USE"
        assert runtime.active_record("demo.gateway") == before
        assert runtime.user_handler_count == 2
        assert (root / "manifest.json").read_bytes() == original
        assert released == []
        assert (await manager._report.read_packages("demo.gateway"))["demo.gateway"].pending is None

        assert (await manager.set_enabled("demo.consumer", False)).status == "SUCCESS"
        assert (await manager.set_enabled("demo.gateway", False)).status == "SUCCESS"
        consumer_root = manager.gateway_installed("demo.consumer")[0].root
        original = (consumer_root / "manifest.json").read_bytes()
        report = await manager.set_enabled("demo.consumer", True)
        assert report.status == "FAILED"
        assert report.error.code == "EXTENSION_DEPENDENCY_NOT_MET"
        assert runtime.user_handler_count == 0
        assert (consumer_root / "manifest.json").read_bytes() == original
        state = (await manager._report.read_packages("demo.consumer"))["demo.consumer"]
        assert state.pending is None
        assert state.active_version is None
    finally:
        await manager.close()
        await runtime.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
async def test_reported_enabled_write_failure_keeps_previous_state(tmp_path, monkeypatch, enabled):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        if enabled:
            assert (await manager.set_enabled("demo.gateway", False)).status == "SUCCESS"
        before = runtime.active_record("demo.gateway")
        root = manager.gateway_installed("demo.gateway")[0].root
        original = (root / "manifest.json").read_bytes()

        def fail_write(*args):
            raise OSError("manifest write failed")

        monkeypatch.setattr(manager._store, "set_enabled", fail_write)
        report = await manager.set_enabled("demo.gateway", enabled)

        assert report.status == "FAILED"
        assert runtime.active_record("demo.gateway") == before
        assert runtime.user_handler_count == int(not enabled)
        assert (root / "manifest.json").read_bytes() == original
        state = (await manager._report.read_packages("demo.gateway"))["demo.gateway"]
        assert state.pending is None
        assert state.active_version == (None if enabled else "0.1.0")
    finally:
        await manager.close()
        await runtime.shutdown()


async def test_reported_failed_enable_restores_disabled_manifest(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        assert (await manager.set_enabled("demo.gateway", False)).status == "SUCCESS"
        root = manager.gateway_installed("demo.gateway")[0].root
        (root / "extension.py").write_text(BAD_ENTRY, encoding="utf-8")

        report = await manager.set_enabled("demo.gateway", True)

        assert report.status == "FAILED"
        assert report.error.code == "EXTENSION_LOAD_FAILED"
        assert runtime.user_handler_count == 0
        assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["enable"] is False
        state = (await manager._report.read_packages("demo.gateway"))["demo.gateway"]
        assert state.pending is None
        assert state.active_version is None
    finally:
        await manager.close()
        await runtime.shutdown()


async def test_reported_install_records_generation_and_domain(tmp_path):
    manager = await _reported_manager(tmp_path)
    try:
        reports = await manager.install([_package_zip(tmp_path)])

        assert [report.status for report in reports] == ["SUCCESS"]
        assert reports[0].current_generation is not None
        assert manager.query()["active"][0]["generation"] == reports[0].current_generation
        assert await manager.get_package_domain("demo.gateway") == "gateway"
        states = await manager._report.read_packages("demo.gateway")
        assert states["demo.gateway"].active_version == "0.1.0"
        assert states["demo.gateway"].pending is None
    finally:
        await manager.close()


async def test_reported_reload_keeps_the_running_copy_on_failure(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        before = runtime.active_record("demo.gateway")
        root = manager.gateway_installed("demo.gateway")[0].root
        (root / "extension.py").write_text(BAD_ENTRY, encoding="utf-8")

        report = await manager.reload("demo.gateway")

        assert report.status == "FAILED"
        after = runtime.active_record("demo.gateway")
        assert after is not None
        assert after.generation == before.generation
        assert runtime.user_handler_count == 1
        states = await manager._report.read_packages("demo.gateway")
        assert states["demo.gateway"].active_version == "0.1.0"
        assert states["demo.gateway"].pending is None
    finally:
        await manager.close()


async def test_reported_uninstall_is_refused_while_handlers_run(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        started, resume = asyncio.Event(), asyncio.Event()
        original = runtime.intercept

        async def slow(event, ops, next_handler):
            started.set()
            await resume.wait()
            return await next_handler(event)

        root = manager.gateway_installed("demo.gateway")[0].root
        runtime.unpublish("demo.gateway")
        runtime.publish(
            LoadedPackage(package_id="demo.gateway", version="0.1.0", root=root, module_names=(),
                          handlers=(HandlerDeclaration("gateway.request", slow, {}, "slow"),))
        )
        request = asyncio.create_task(original(SimpleNamespace(closed=False), "req", "ping", {}))
        await started.wait()

        report = await manager.uninstall("demo.gateway")

        assert report.status == "FAILED"
        assert report.error.code == "EXTENSION_PACKAGE_BUSY"
        assert manager.gateway_installed("demo.gateway")
        resume.set()
        await request
    finally:
        await manager.close()


async def test_reported_startup_loads_only_gateway_packages(tmp_path):
    first = await _reported_manager(tmp_path)
    await first.install([_package_zip(tmp_path)])
    await first.close()
    agent_root = tmp_path / "extension_pkgs" / "demo.agent" / "0.1.0"
    agent_root.mkdir(parents=True)
    agent_root.joinpath("manifest.json").write_text(json.dumps({
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": "demo.agent", "version": "0.1.0", "domain": "agentserver"},
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }), encoding="utf-8")
    agent_root.joinpath("extension.py").write_text(ENTRY, encoding="utf-8")

    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        reports = await manager.load_installed()

        assert [(report.package_id, report.status) for report in reports] == [("demo.gateway", "SUCCESS")]
        assert [package.package_id for package in runtime.active_packages()] == ["demo.gateway"]
        assert {package.package_id for package in manager.installed()} == {"demo.gateway", "demo.agent"}
    finally:
        await manager.close()


async def test_reported_uninstall_finishes_after_the_caller_is_cancelled(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        await manager.install([_package_zip(tmp_path)])
        entered = asyncio.Event()
        original = runtime.cleanup_package_data

        async def slow_cleanup(package_id: str) -> None:
            entered.set()
            await asyncio.sleep(0.1)
            await original(package_id)

        monkeypatch.setattr(runtime, "cleanup_package_data", slow_cleanup)
        task = asyncio.create_task(manager.uninstall("demo.gateway"))
        await entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.gateway_installed("demo.gateway") == []
        assert runtime.active_record("demo.gateway") is None
        states = await manager._report.read_packages("demo.gateway")
        state = states.get("demo.gateway")
        assert state is None or (state.pending is None and state.active_version is None)
    finally:
        await manager.close()


def _installed_package(tmp_path: Path, package_id: str, domain: str | None) -> Path:
    root = tmp_path / "extension_pkgs" / package_id / "0.1.0"
    root.mkdir(parents=True)
    package: dict[str, str] = {"id": package_id, "version": "0.1.0"}
    if domain is not None:
        package["domain"] = domain
    root.joinpath("manifest.json").write_text(json.dumps({
        "package_type": "extension",
        "schemaVersion": 1,
        "package": package,
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }), encoding="utf-8")
    root.joinpath("extension.py").write_text(ENTRY, encoding="utf-8")
    return root


async def test_reported_startup_rejects_packages_outside_the_host_domains(tmp_path):
    _installed_package(tmp_path, "demo.gateway", "gateway")
    _installed_package(tmp_path, "demo.agent", "agentserver")
    _installed_package(tmp_path, "demo.nodomain", None)
    _installed_package(tmp_path, "demo.other", "other")

    runtime = await _runtime(tmp_path)
    manager = await _reported_manager(tmp_path, runtime)
    try:
        reports = await manager.load_installed()

        assert [(report.package_id, report.status) for report in reports] == [("demo.gateway", "SUCCESS")]
        report = manager._report
        data = report._read()
        assert sorted(data["startup_order"]) == ["demo.agent", "demo.gateway"]
        assert {"demo.nodomain", "demo.other"}.isdisjoint(data["packages"])
        assert {package.package_id for package in manager.installed()} == {
            "demo.gateway", "demo.agent", "demo.nodomain", "demo.other",
        }
        assert [package.package_id for package in runtime.active_packages()] == ["demo.gateway"]
    finally:
        await manager.close()


async def test_reported_startup_order_is_shared_across_both_domains(tmp_path):
    _installed_package(tmp_path, "demo.gateway", "gateway")
    _installed_package(tmp_path, "demo.agent", "agentserver")
    manager = await _reported_manager(tmp_path)
    try:
        await manager.load_installed()
    finally:
        await manager.close()

    from openjiuwen.core.extension.install_report import PackageInstallReport
    from openjiuwen.core.extension.package_manager import PackageManager

    agent_report = PackageInstallReport(_report_path(tmp_path), poll_interval=0.05, dependency_timeout=5.0, lock_timeout=2.0)
    agent_store = PackageManager(
        tmp_path / "extension_pkgs", domain="agentserver", allowed_domains=("gateway", "agentserver"),
    )
    agent_store.install_report = agent_report
    await agent_report.open(agent_store.store, domain="agentserver")
    try:
        plan = await agent_report.prepare_startup(agent_store)

        assert [(manifest.package_id, manifest.domain) for manifest in plan.ordered] == [
            ("demo.agent", "agentserver"),
            ("demo.gateway", "gateway"),
        ]
        assert plan.rejected == ()
    finally:
        await agent_report.close()


async def test_gateway_assembly_survives_an_invalid_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19002")
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    conflicting = {
        "extensions": {
            "install_dir": str(tmp_path / "shared"),
            "gateway": {"install_dir": str(tmp_path / "legacy")},
        }
    }

    result = await assembly.install_gateway_extensions(Channel(), conflicting)

    assert result is None
    assert assembly._ASSEMBLY is None

    from jiuwenswarm.gateway.extensions_v2.splash_hint import installed_splash_providers

    def broken_config(*args, **kwargs):
        raise ValueError("bad config")

    monkeypatch.setattr("jiuwenswarm.gateway.extensions_v2.splash_hint.load_gateway_config", broken_config)
    assert installed_splash_providers() == []
