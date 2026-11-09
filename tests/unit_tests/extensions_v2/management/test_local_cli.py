# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise CLI entry points and real local transport against live managers."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from openjiuwen.core.events import Event

from jiuwenswarm.channels.cli import extension as cli
from jiuwenswarm.common import extension_cli as transport
from jiuwenswarm.extensions_v2.management import adapter as adapter_module
from jiuwenswarm.extensions_v2.management.local_cli import handle
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

from .support import METADATA_EXTENSION, archive_package, make_bus, write_package

ROOT = Path(__file__).resolve().parents[4]
PACKAGE_ID = "test.pkg"


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith(transport.ENV_PREFIX):
            monkeypatch.delenv(key)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")
    monkeypatch.setenv("GATEWAY_PORT", "19001")


async def invoke(action: str, target: str = "", *options: str) -> tuple[dict[str, Any], int]:
    values = [action, *([target] if target else []), *options]
    return await cli.execute(cli.parser().parse_args(values))


@pytest.mark.asyncio
async def test_cli_real_zip_install_reload_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ExtensionManager(tmp_path / "installed", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_extension_manager", lambda: manager)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime",
                        lambda: SimpleNamespace(notify_packages_changed=notify))
    package = write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    archive = archive_package(package)
    service = transport.LocalControl("agent_server", handle)
    await service.start()
    try:
        result, code = await invoke("install", str(archive), "--json")
        assert code == 0
        assert result["ok"]
        assert result["domain"] == "agentserver"
        assert result["verification_status"] == "SUCCESS"
        assert result["ui_notified"] is True
        notify.assert_awaited_once_with([PACKAGE_ID])
        root = Path(result["install_path"])
        assert root == manager.install_dir / PACKAGE_ID / "1.0.0"
        assert (await manager.registry.context.bus.trigger(Event("test.event", {})))[0] == PACKAGE_ID
        (root / "extension.py").write_text(
            METADATA_EXTENSION.replace(
                "return (metadata.package_id, metadata.package_version, str(metadata.root))", 'return "reloaded"',
            ), encoding="utf-8",
        )
        result, code = await invoke("reload", PACKAGE_ID)
        assert code == 0 and result["state"]["active"][0]["generation"] == 2
        assert await manager.registry.context.bus.trigger(Event("test.event", {})) == "reloaded"
        result, code = await invoke("uninstall", PACKAGE_ID)
        assert code == 0 and not result["active"] and not root.exists()
        notify.assert_awaited_once_with([PACKAGE_ID])
    finally:
        await service.close()
        await manager.shutdown()


class Peer:
    def __init__(self, mode: str = "success", package_id: str = PACKAGE_ID) -> None:
        self.mode = mode
        self.package_id = package_id
        self.active = False
        self.writes = 0
        self.queries = 0

    def state(self, package_id: str | None = None) -> dict[str, Any]:
        owns_package = package_id in (None, self.package_id)
        record = {
            "package_id": self.package_id,
            "version": "1.0.0",
            "generation": 1,
            "root": f"/installed/{self.package_id}",
        }
        return {
            "active": [record] if self.active and owns_package else [],
            "installed": [record["root"]] if self.active and owns_package else [],
            "installed_packages": [record] if self.active and owns_package else [],
        }

    async def handle(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "query":
            self.queries += 1
            if self.mode == "query_failed" and self.writes:
                return {"ok": False, "payload": {"code": "QUERY_FAILED", "error": "query failed"}}
            return {"ok": True, "payload": {"state": self.state(params.get("package_id"))}}
        self.writes += 1
        self.active = action != "uninstall"
        report = {
            "package_id": str(params.get("package_id") or self.package_id),
            "operation": action,
            "status": "SUCCESS",
            "current_generation": 1,
        }
        if self.mode == "failed":
            report.update(status="FAILED", error={"code": "LOAD_FAILED"})
        elif self.mode == "timeout":
            await asyncio.sleep(0.2)
        elif self.mode == "unknown":
            return {"ok": False, "payload": {"outcome": "unknown", "code": "INTERNAL_ERROR"}}
        elif self.mode == "invalid_report":
            report["package_id"] = "other.pkg"
        elif self.mode == "state_mismatch":
            report["current_generation"] = 2
        return {"ok": True, "payload": {"reports": [report]}}


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "中文 package.zip"
    with ZipFile(path, "w") as output:
        output.writestr("manifest.json", '{"package": {"domain": "agentserver", "id": "test.pkg", "version": "1.0.0"}}')
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code,status", [
    ("success", 0, "SUCCESS"), ("failed", 1, "FAILED"), ("state_mismatch", 1, "FAILED"),
    ("unknown", 3, "UNKNOWN"), ("invalid_report", 3, "UNKNOWN"), ("timeout", 3, "UNKNOWN"),
    ("query_failed", 3, "UNKNOWN"),
])
async def test_reports_recovery_and_no_write_retry(archive: Path, mode: str, code: int, status: str) -> None:
    peer = Peer(mode)
    service = transport.LocalControl("agent_server", peer.handle)
    await service.start()
    try:
        result, actual_code = await invoke("install", str(archive), "--timeout", "0.06")
        assert actual_code == code and result["status"] == status
        assert peer.writes == 1
        if status == "UNKNOWN":
            assert not result["ok"]
        if mode in {"unknown", "invalid_report", "timeout"}:
            assert peer.queries == 2 and result["state"]["active"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_conflict_does_not_uninstall_or_overwrite(archive: Path) -> None:
    peer = Peer()
    peer.active = True
    service = transport.LocalControl("agent_server", peer.handle)
    await service.start()
    try:
        result, code = await invoke("install", str(archive))
        assert code == 2 and result["error"]["code"] == "PACKAGE_ALREADY_PRESENT" and peer.writes == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bad_capability_is_rejected_before_query(archive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = Peer()
    service = transport.LocalControl("agent_server", peer.handle)
    await service.start()
    try:
        context = transport.read_context("agent_server")
        context["token"] = "x" * 43
        monkeypatch.setenv(transport.ENV_PREFIX + "AGENT_SERVER", json.dumps(context))
        result, code = await invoke("install", str(archive))
        assert code == 2 and result["error"]["code"] == "AUTH_REQUIRED"
        assert peer.writes == peer.queries == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_instance_capabilities_do_not_cross() -> None:
    first_peer, second_peer = Peer(), Peer()
    first = transport.LocalControl("agent_server", first_peer.handle)
    second = transport.LocalControl("agent_server", second_peer.handle)
    await first.start()
    first_context = transport.read_context("agent_server")
    await second.start()
    try:
        second_context = transport.read_context("agent_server")
        forged = {**first_context, "port": second_context["port"]}
        response = await transport.request(forged, "query", {}, 1)
        assert not response["ok"] and second_peer.queries == 0
        result, code = await invoke("query")
        assert code == 0 and second_peer.queries == 1 and first_peer.queries == 0
        await first.close()
        assert transport.read_context("agent_server") == second_context
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_same_instance_record_supports_sibling_host(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = Peer()
    service = transport.LocalControl("gateway", peer.handle)
    await service.start()
    path = transport.context_path("gateway")
    try:
        monkeypatch.delenv(transport.ENV_PREFIX + "GATEWAY")
        result, code = await invoke("query", "", "--domain", "gateway")
        assert code == 0 and result["domain"] == "gateway" and peer.queries == 1
        monkeypatch.setenv("GATEWAY_PORT", "19002")
        result, code = await invoke("query", "", "--domain", "gateway")
        assert code == 2 and result["error"]["code"] == "HOST_UNAVAILABLE" and peer.queries == 1
    finally:
        await service.close()
    assert not path.exists()


@pytest.mark.asyncio
async def test_default_query_merges_active_packages_from_both_hosts() -> None:
    agent_peer = Peer(package_id="demo.agent")
    gateway_peer = Peer(package_id="demo.gateway")
    agent_peer.active = gateway_peer.active = True
    agent = transport.LocalControl("agent_server", agent_peer.handle)
    gateway = transport.LocalControl("gateway", gateway_peer.handle)
    await agent.start()
    await gateway.start()
    try:
        result, code = await invoke("query")
        assert code == 0 and result["domain"] == "all"
        assert result["domains"] == ["agentserver", "gateway"]
        assert {record["package_id"] for record in result["state"]["active"]} == {
            "demo.agent", "demo.gateway",
        }
        assert agent_peer.queries == gateway_peer.queries == 1
    finally:
        await gateway.close()
        await agent.close()


@pytest.mark.asyncio
async def test_default_mutations_follow_web_owner_routing() -> None:
    agent_peer = Peer(package_id="demo.agent")
    gateway_peer = Peer(package_id="demo.gateway")
    agent_peer.active = gateway_peer.active = True
    agent = transport.LocalControl("agent_server", agent_peer.handle)
    gateway = transport.LocalControl("gateway", gateway_peer.handle)
    await agent.start()
    await gateway.start()
    try:
        result, code = await invoke("reload", "demo.gateway")
        assert code == 0 and result["domain"] == "gateway"
        assert gateway_peer.writes == 1 and agent_peer.writes == 0

        result, code = await invoke("uninstall", "demo.agent")
        assert code == 0 and result["domain"] == "agentserver"
        assert gateway_peer.writes == 1 and agent_peer.writes == 1
    finally:
        await gateway.close()
        await agent.close()


@pytest.mark.asyncio
async def test_default_package_query_follows_web_owner_routing() -> None:
    agent_peer = Peer(package_id="demo.agent")
    gateway_peer = Peer(package_id="demo.gateway")
    agent_peer.active = gateway_peer.active = True
    agent = transport.LocalControl("agent_server", agent_peer.handle)
    gateway = transport.LocalControl("gateway", gateway_peer.handle)
    await agent.start()
    await gateway.start()
    try:
        result, code = await invoke("query", "demo.gateway")
        assert code == 0 and result["domain"] == "gateway"
        assert result["state"]["active"][0]["package_id"] == "demo.gateway"
        assert gateway_peer.queries == 1 and agent_peer.queries == 0
    finally:
        await gateway.close()
        await agent.close()


@pytest.mark.asyncio
async def test_subprocess_uses_inherited_context_and_utf8(archive: Path, tmp_path: Path) -> None:
    peer = Peer()
    service = transport.LocalControl("agent_server", peer.handle)
    await service.start()
    try:
        environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "jiuwenswarm.channels.cli.main", "extension", "install", str(archive), "--json",
            cwd=tmp_path, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        result = json.loads(stdout.decode("utf-8"))
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert result["ok"] and result["zip_path"] == str(archive)
        assert transport.read_context("agent_server")["token"].encode() not in stdout + stderr
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_no_context_is_reported_without_port_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SERVER_PORT")
    monkeypatch.delenv("AGENT_PORT", raising=False)
    result, code = await invoke("query")
    assert code == 2 and result["error"]["code"] == "HOST_UNAVAILABLE"


@pytest.mark.asyncio
async def test_manifest_selects_gateway_host(archive: Path) -> None:
    with ZipFile(archive, "w") as output:
        output.writestr("manifest.json", '{"package": {"domain": "gateway", "id": "test.pkg", "version": "1.0.0"}}')
    agent_peer, gateway_peer = Peer(), Peer()
    agent = transport.LocalControl("agent_server", agent_peer.handle)
    gateway = transport.LocalControl("gateway", gateway_peer.handle)
    await agent.start()
    await gateway.start()
    try:
        result, code = await invoke("install", str(archive))
        assert code == 0 and result["domain"] == "gateway"
        assert gateway_peer.writes == 1 and agent_peer.writes == 0
        result, code = await invoke("install", str(archive), "--domain", "agentserver")
        assert code == 2 and result["error"]["code"] == "DOMAIN_MISMATCH"
    finally:
        await gateway.close()
        await agent.close()


@pytest.mark.parametrize("frozen", [False, True])
def test_frozen_entry_dispatches_before_single_instance_lock(
    monkeypatch: pytest.MonkeyPatch, frozen: bool,
) -> None:
    tree = ast.parse((ROOT / "scripts/jiuwenswarm_exe_entry.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_dispatch")
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 3)
    monkeypatch.setattr(sys, "argv", ["workswarm", "extension", "query", "--json"])
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    namespace = {"sys": sys, "_pop_flag": lambda flag: False, "_ensure_stdio": lambda: None,
                 "_DESKTOP_INSTALL_EXTERNAL_CLI": "install-cli", "_DESKTOP_RESET_EXTERNAL_CLI_CONFIG": "reset-cli"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "frozen_dispatch", "exec"), namespace)
    assert namespace["_dispatch"]() == 3
    assert calls == [["query", "--json"]]


@pytest.mark.asyncio
async def test_ready_owner_is_notified_and_disconnect_releases_it() -> None:
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
    from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

    class Push:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send_push(self, message: dict[str, Any]) -> bool:
            self.messages.append(message)
            return True

    push = Push()
    runtime = WebUIRuntime(bus=make_bus(), transport=RenderTransport(push=push))
    runtime.remember_ready_client("ws-1", "session-1")
    assert await runtime.notify_packages_changed([PACKAGE_ID])
    message = push.messages[0]
    assert message["metadata"] == {"ws_id": "ws-1"}
    assert message["payload"]["package_ids"] == [PACKAGE_ID]
    await runtime.drop_connection("ws-1")
    assert not await runtime.notify_packages_changed([PACKAGE_ID])
    assert len(push.messages) == 1


@pytest.mark.asyncio
async def test_agent_control_rejects_gateway_zip_before_loading(tmp_path: Path) -> None:
    root = write_package(tmp_path / "foreign")
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["package"]["domain"] = "gateway"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    response = await handle("install", {"paths": [str(archive_package(root))]})
    assert not response["ok"] and response["payload"]["code"] == "DOMAIN_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["--timeout=nan", "--timeout=inf", "--timeout=0"])
async def test_invalid_timeout_is_rejected_before_connecting(option: str) -> None:
    result, code = await invoke("query", "", option)
    assert code == 2 and result["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_invalid_zip_is_rejected_without_host_lookup(tmp_path: Path) -> None:
    archive = tmp_path / "invalid.zip"
    archive.write_bytes(b"not a ZIP")
    result, code = await invoke("install", str(archive))
    assert code == 2 and result["error"]["code"] == "INVALID_ZIP"


@pytest.mark.asyncio
async def test_closed_host_revokes_context() -> None:
    service = transport.LocalControl("agent_server", Peer().handle)
    await service.start()
    path = transport.context_path("agent_server")
    assert path.exists()
    await service.close()
    assert not path.exists() and transport.ENV_PREFIX + "AGENT_SERVER" not in os.environ
    result, code = await invoke("query")
    assert code == 2 and result["error"]["code"] == "HOST_UNAVAILABLE"


@pytest.mark.asyncio
async def test_host_shutdown_drains_cli_requests_before_releasing_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.extensions_v2 import assembly

    manager = ExtensionManager(tmp_path / "installed", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_query(action: str, params: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"ok": manager.initialized, "payload": {}}

    class Control(transport.LocalControl):
        async def close(self) -> None:
            closing.set()
            await super().close()

    service = Control("agent_server", blocked_query)
    await service.start()
    monkeypatch.setattr(assembly, "_LOCAL_CLI", service)
    monkeypatch.setattr(assembly, "_ASSEMBLY", SimpleNamespace(
        manager=manager, compact=SimpleNamespace(close=AsyncMock()),
        authorization=SimpleNamespace(close=lambda: None), webui=SimpleNamespace(shutdown=AsyncMock()),
    ))
    request = asyncio.create_task(transport.request(transport.read_context("agent_server"), "query", {}, 2))
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(assembly.shutdown_extensions())
        await asyncio.wait_for(closing.wait(), 2)
        release.set()
        assert (await request)["ok"]
        await shutdown
        assert not manager.initialized
        assert transport.ENV_PREFIX + "AGENT_SERVER" not in os.environ
    finally:
        release.set()
        await asyncio.gather(request, *([shutdown] if shutdown is not None else []), return_exceptions=True)
        await service.close()
        await manager.shutdown()


@pytest.mark.parametrize("domain", [None, "agent", "GATEWAY", " gateway ", "other", 1])
def test_cli_rejects_missing_or_illegal_package_domain(tmp_path, domain):
    path = tmp_path / "invalid-domain.zip"
    package = {"id": "test.pkg", "version": "1.0.0"}
    if domain is not None:
        package["domain"] = domain
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"package": package}))
    with pytest.raises(cli.ClientError, match="package.domain"):
        cli.inspect_zip(str(path))


def test_cli_default_timeout_tracks_dependency_budget(monkeypatch):
    from jiuwenswarm.extensions_v2 import config
    monkeypatch.setattr(config, "resolve_install_report_settings", lambda: SimpleNamespace(dependency_timeout=600))
    monkeypatch.setattr(transport, "read_context", lambda domain: {"domain": domain})
    args = cli.parser().parse_args(["query"])
    settings = cli.Settings.from_args(args)
    assert settings.timeout == 630
