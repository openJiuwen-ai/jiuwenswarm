# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm CLI contracts against the installed external Gateway extension host.

Extracted from releases/0.2.9.beta3 at e4d3b811c5289a4181b26e9f907b535a707ecc1c:
  tests/unit_tests/gateway/extensions_v2/test_gateway_package_management.py
  tests/unit_tests/gateway/extensions_v2/test_presentation_control.py
Only Swarm CLI and local-control interoperability stays in this repository.
Gateway runtime, package manager, assembly and local RPC transport are real.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agent_gateway.extensions_v2 import assembly
from agent_gateway.extensions_v2.config import GatewayExtensionConfig
from agent_gateway.extensions_v2.loader import LoadedPackage
from agent_gateway.extensions_v2.management.local_cli import start_local_cli
from agent_gateway.extensions_v2.runtime import GatewayRuntime
from agent_gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput
from jiuwenswarm.channels.cli import extension as cli
from jiuwenswarm.channels.cli import presentation as presentation_cli
from jiuwenswarm.common import extension_cli

pytestmark = pytest.mark.asyncio


class Channel:
    """Capture the browser transport while keeping Gateway host behavior real."""

    channel_id = "web"

    def __init__(self) -> None:
        self.sockets: dict[str, object] = {}
        self.events: list[tuple[str, str, dict]] = []
        self.broadcasts: list[tuple[str, dict]] = []

    async def ensure_ws_id(self, ws) -> str:
        connection_id = getattr(ws, "_jiuwen_ws_id", "") or ws.name
        ws._jiuwen_ws_id = connection_id
        self.sockets[connection_id] = ws
        return connection_id

    def ws_by_id(self, connection_id: str) -> object | None:
        return self.sockets.get(connection_id)

    async def send_event(self, ws, event: str, payload: dict) -> None:
        self.events.append((ws._jiuwen_ws_id, event, payload))

    async def broadcast_event(self, event: str, payload: dict) -> None:
        self.broadcasts.append((event, payload))


@pytest_asyncio.fixture(autouse=True)
async def isolate_control_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    await assembly.shutdown_gateway_extensions()
    # Both independently packaged local transports must publish into this test workspace.
    monkeypatch.setattr("jiuwenswarm.common.paths.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr("agent_gateway.common.paths.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")
    monkeypatch.delenv("JIUWENSWARM_EXTENSION_CLI_GATEWAY", raising=False)
    monkeypatch.delenv("JIUWENSWARM_EXTENSION_CLI_AGENT_SERVER", raising=False)
    try:
        yield
    finally:
        await assembly.shutdown_gateway_extensions()


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


def _config(tmp_path: Path) -> GatewayExtensionConfig:
    return GatewayExtensionConfig(
        install_dir=tmp_path / "extension_pkgs",
        data_dir=tmp_path / "gateway_data",
        recovery_data_dir=tmp_path / "recovery",
    )


class _FakeAgentServer:
    """Minimal AgentServer RPC for the Gateway CLI forwarding contract."""

    def __init__(self, runtime: GatewayRuntime) -> None:
        from agent_gateway.extensions_v2.management.install_state import ExtensionInstallState

        self.runtime = runtime
        self.install_dir = Path(runtime.config.install_dir)
        self.store = ExtensionInstallState.for_install_dir(self.install_dir)

    @staticmethod
    def _response(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(ok=True, payload=payload)

    @staticmethod
    def _report(
        package_id: str,
        operation: str,
        *,
        status: str = "SUCCESS",
        previous_generation: int | None = None,
        current_generation: int | None = None,
        error: dict | None = None,
    ) -> dict:
        return {
            "package_id": package_id,
            "operation": operation,
            "status": status,
            "plan_id": "",
            "previous_generation": previous_generation,
            "current_generation": current_generation,
            "error": error,
        }

    async def send_request(self, envelope) -> SimpleNamespace:
        method = getattr(envelope, "method", "")
        params = getattr(envelope, "params", {}) or {}
        if method == "extensions.v2.install":
            return self._install(params)
        if method == "extensions.v2.reload":
            return self._reload(params)
        if method == "extensions.v2.uninstall":
            return self._uninstall(params)
        return SimpleNamespace(
            ok=False,
            payload={"error": f"unsupported {method}", "code": "BAD_REQUEST"},
        )

    def _install(self, params: dict) -> SimpleNamespace:
        from agent_gateway.extensions_v2.management.install_state import (
            ActiveInstance,
            ExtensionInstallRecord,
        )

        source = Path(params["paths"][0])
        with zipfile.ZipFile(source) as archive:
            document = json.loads(archive.read("manifest.json"))
            entry = archive.read("extension.py").decode("utf-8")
            package = document["package"]
            package_id = package["id"]
            version = package["version"]
            target = self.install_dir / package_id / version
            shutil.rmtree(target, ignore_errors=True)
            archive.extractall(target)
        if "not.a.gateway.event" in entry:
            return self._response(
                {
                    "reports": [
                        self._report(
                            package_id,
                            "install",
                            status="FAILED",
                            error={
                                "code": "EXTENSION_LOAD_FAILED",
                                "message": "bad gateway event",
                            },
                        )
                    ]
                }
            )
        self.store.upsert(
            ExtensionInstallRecord(
                package_id=package_id, version=version, origin="user",
                install_path=str(target), active=True, enabled=True,
                domain="gateway", status="pending",
            )
        )
        self.store.switch_active_instance(
            ActiveInstance(package_id=package_id, version=version, kind="standalone")
        )
        self.store.mark(package_id, version=version, status="pending")
        return self._response(
            {
                "reports": [
                    self._report(package_id, "install", current_generation=1)
                ],
                "installed_packages": [
                    {
                        "package_id": package_id, "version": version,
                        "install_path": str(target), "generation": 1,
                    }
                ],
            }
        )

    def _reload(self, params: dict) -> SimpleNamespace:
        package_id = params["package_id"]
        record = self.store.get(package_id)
        current = self.runtime.active_record(package_id)
        next_generation = (current.generation + 1) if current is not None else 1
        self.store.mark(
            package_id,
            version=record.version if record is not None else "",
            status="pending",
        )
        return self._response(
            {
                "reports": [
                    self._report(
                        package_id,
                        "reload",
                        previous_generation=(
                            current.generation if current is not None else None
                        ),
                        current_generation=next_generation,
                    )
                ]
            }
        )

    def _uninstall(self, params: dict) -> SimpleNamespace:
        package_id = params["package_id"]
        record = self.store.get(package_id)
        self.store.mark(
            package_id,
            version=record.version if record is not None else "",
            active=False, enabled=False, status="uninstalling",
        )
        return self._response(
            {"reports": [self._report(package_id, "uninstall")]}
        )


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=Channel(), config=_config(tmp_path))
    await runtime.start()
    runtime.channel.agent_client = _FakeAgentServer(runtime)
    return runtime


def _package_zip(tmp_path: Path, *, entry: str = ENTRY) -> Path:
    document = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": "demo.gateway", "version": "0.1.0", "domain": "gateway"},
        "extensions": [{"id": "demo", "runtime": "backend", "module": "extension.py"}],
    }
    archive = tmp_path / "demo.gateway-0.1.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(document))
        bundle.writestr("extension.py", entry)
    return archive


# Source: test_gateway_package_management.py::test_local_cli_uses_live_gateway_manager.
async def test_local_cli_uses_live_gateway_manager(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    try:
        archive = _package_zip(tmp_path)
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


# Source: test_gateway_package_management.py::test_local_cli_reload_asks_mounted_slots_for_ready.
async def test_local_cli_reload_asks_mounted_slots_for_ready(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    try:
        archive = _package_zip(tmp_path)
        await cli.execute(cli.parser().parse_args(["install", str(archive), "--json"]))
        result, code = await cli.execute(cli.parser().parse_args(["reload", "demo.gateway", "--domain", "gateway"]))
        assert code == 0 and result["verification_status"] == "SUCCESS"
        assert runtime.channel.broadcasts == [("extensions.v2.installed", {"package_ids": ["demo.gateway"]})] * 2
        await cli.execute(cli.parser().parse_args(["uninstall", "demo.gateway", "--domain", "gateway"]))
        assert len(runtime.channel.broadcasts) == 2
    finally:
        await service.close()
        await runtime.shutdown()


# Source: test_gateway_package_management.py::test_local_cli_retains_failed_gateway_report.
async def test_local_cli_retains_failed_gateway_report(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    service = await start_local_cli(runtime)
    try:
        archive = _package_zip(tmp_path, entry=BAD_ENTRY)
        result, code = await cli.execute(cli.parser().parse_args(["install", str(archive)]))
        assert code == 1 and result["operation_status"] == "FAILED"
        assert isinstance(result["reports"][0]["error"]["code"], str)
        assert not runtime.active_packages()
    finally:
        await service.close()
        await runtime.shutdown()


# Source: test_gateway_package_management.py::test_gateway_assembly_publishes_and_revokes_cli.
async def test_gateway_assembly_publishes_and_revokes_cli(tmp_path: Path) -> None:
    assert assembly.get_gateway_assembly() is None
    try:
        first = await assembly.install_gateway_extensions(Channel(), _config(tmp_path))
        assert first is not None
        result, code = await cli.execute(cli.parser().parse_args(["query", "--domain", "gateway"]))
        assert code == 0 and result["state"]["active"] == []
    finally:
        await assembly.shutdown_gateway_extensions()
    result, code = await cli.execute(cli.parser().parse_args(["query", "--domain", "gateway"]))
    assert code == 2 and result["error"]["code"] == "HOST_UNAVAILABLE"


# Source: test_presentation_control.py::test_cli_uses_gateway_context_and_preserves_unknown.
async def test_cli_uses_gateway_context_and_preserves_unknown(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    service = None
    try:
        root = tmp_path / "presentation"
        (root / "assets").mkdir(parents=True)
        (root / "assets" / "presentation.html").write_text("<!doctype html><title>Bee</title>", encoding="utf-8")
        runtime.publish(LoadedPackage(
            package_id="test.forms", version="1.0.0", root=root, handlers=(), domain="gateway",
            ui={"apiVersion": 1, "items": [{
                "type": "web.slot.contribution", "slot": "application.presentation",
            }]},
        ))
        ws = SimpleNamespace(name="window-1")
        await runtime.channel.ensure_ws_id(ws)
        activation = runtime.current_activation("test.forms")
        target = RenderTarget("", "application.presentation", "test.forms", "bee", "application", "window-1")
        source = f"/extension-assets/test.forms/{activation.generation}/presentation.html"
        async with runtime.registry.claim(target) as claim:
            runtime.registry.commit(claim.instance, UiRenderInput(target, RenderNode("ApplicationSurface", {
                "title": {"zh": "蜜蜂", "en": "Bee"}, "src": source,
            })))
            revision = claim.instance.revision
        # A real Gateway deadline yields UNKNOWN when the browser sends no receipt.
        runtime.browser.presentations.timeout = 0.01
        service = await start_local_cli(runtime)
        params = {"connection_id": "window-1", "package_id": "test.forms", "instance_key": "bee", "revision": revision}
        result, status = await presentation_cli.execute(presentation_cli.parser().parse_args(["query", "test.forms", "--json"]))
        assert status == 0 and result["result"]["candidates"] == [{**params, "title": {"zh": "蜜蜂", "en": "Bee"}}]
        result, status = await presentation_cli.execute(presentation_cli.parser().parse_args([
            "start", "test.forms", "bee", "--connection-id", "window-1", "--revision", str(revision), "--json",
        ]))
        assert status == 3 and result["status"] == "UNKNOWN"
        starts = [frame for frame in runtime.channel.events if frame[1] == "webui.presentation.start"]
        assert len(starts) == 1 and starts[0][0] == "window-1"
        assert not runtime.browser.presentations.pending
    finally:
        if service is not None:
            await service.close()
        await runtime.shutdown()


# Source: test_presentation_control.py::test_default_local_control_does_not_accept_presentation_methods.
async def test_default_local_control_does_not_accept_presentation_methods() -> None:
    handle = AsyncMock()
    service = extension_cli.LocalControl("agent_server", handle)
    await service.start()
    try:
        response = await extension_cli.request(extension_cli.read_context("agent_server"), "presentation.query", {}, 1)
        assert not response["ok"]
        handle.assert_not_awaited()
    finally:
        await service.close()
