# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway package management over the shared ``extension_pkgs`` store."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ExtensionFailure

from jiuwenswarm.gateway.channel_manager.web import extension_package_handlers
from jiuwenswarm.gateway.extensions_v2 import assembly
from jiuwenswarm.gateway.extensions_v2.builtin.auth_gate import AUTH_STORE_KEY
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager
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


def _package_zip(
    tmp_path: Path,
    *,
    package_id: str = "demo.gateway",
    domain: str = "gateway",
    version: str = "0.1.0",
    auth_provider: bool = False,
    entry: str = ENTRY,
) -> Path:
    source = tmp_path / "src" / package_id / version
    source.mkdir(parents=True)
    manifest = (
        "schemaVersion: 1\n"
        "package:\n"
        f"  id: {package_id}\n"
        f"  version: {version}\n"
        f"domain: {domain}\n"
        f"auth_provider: {'true' if auth_provider else 'false'}\n"
        "extensions:\n"
        "  - id: demo\n"
        "    runtime: backend\n"
        "    module: extension.py\n"
    )
    (source / "manifest.yaml").write_text(manifest, encoding="utf-8")
    (source / "extension.py").write_text(entry, encoding="utf-8")
    archive = tmp_path / f"{package_id}-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in source.iterdir():
            bundle.write(file, file.name)
    return archive


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(
        channel=Channel(),
        config=GatewayExtensionConfig(install_dir=tmp_path / "extension_pkgs"),
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
    assert state["active"][0]["version"] == "0.1.0"

    report = await manager.uninstall("demo.gateway")

    assert report.status == "SUCCESS"
    assert runtime.user_handler_count == 0
    assert manager.installed() == []


async def test_agent_package_is_rejected_without_copying(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")

    reports = await manager.install([_package_zip(tmp_path, package_id="demo.agent", domain="agent")])

    assert [report.status for report in reports] == ["FAILED"]
    assert reports[0].error.code == "EXTENSION_SOURCE_INVALID"
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


async def test_auth_provider_flag_follows_install_and_uninstall(tmp_path):
    runtime = await _runtime(tmp_path)
    manager = GatewayPackageManager(runtime, tmp_path / "extension_pkgs")
    assert runtime.has_auth_provider is False

    await manager.install([_package_zip(tmp_path, auth_provider=True)])
    assert runtime.has_auth_provider is True

    await manager.uninstall("demo.gateway")
    assert runtime.has_auth_provider is False


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


async def test_management_requires_login_once_the_gate_is_active(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path)
    await GatewayPackageManager(runtime, tmp_path / "extension_pkgs").install(
        [_package_zip(tmp_path, auth_provider=True)]
    )
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    channel = runtime.channel
    ws = SimpleNamespace()

    handled = await extension_package_handlers._handle_locally(
        channel=channel,
        resolve_client=lambda: None,
        ws=ws,
        req_id="r3",
        params={"package_id": "demo.gateway"},
        session_id=None,
        user_id=None,
        method=extension_package_handlers.ReqMethod.EXTENSIONS_V2_UNINSTALL,
    )

    assert handled is True
    assert channel.responses[0]["code"] == "AUTH_REQUIRED"

    runtime.store.set(
        scope="connection",
        scope_id="ws-1",
        key=AUTH_STORE_KEY,
        value={"token": "tk"},
    )
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

    reports = await manager.install([_package_zip(tmp_path, auth_provider=True, entry=BAD_ENTRY)])

    assert [report.status for report in reports] == ["FAILED"]
    assert reports[0].error.code == "EXTENSION_LOAD_FAILED"
    assert runtime.user_handler_count == 0
    assert runtime.has_auth_provider is False
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
