"""Load the shipped demos in isolated real hosts and inspect rendered frames."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

BASE = Path(__file__).parent
DEV = Path(__file__).resolve().parents[2]


def builder() -> Callable[..., Path]:
    path = DEV / "scripts/build_package.py"
    spec = importlib.util.spec_from_file_location("demo_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_package


def example_archive(source: Path, output: Path) -> Path:
    """Use the finalizer's exact ZIP when validating its selected source."""
    selected_source = os.environ.get("JIUWEN_SYSTEM_PLUGIN_SOURCE")
    selected_zip = os.environ.get("JIUWEN_SYSTEM_PLUGIN_ZIP")
    if selected_source and selected_zip and Path(selected_source).resolve() == source.resolve():
        return Path(selected_zip)
    return builder()(source, output)


class Push:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.ws = SimpleNamespace(closed=False)

    async def send_push(self, message: dict[str, Any]) -> bool:
        self.frames.append(message)
        return True

    def ws_by_id(self, connection: str) -> SimpleNamespace | None:
        return self.ws if connection == "demo-client" else None

    async def send_event(self, ws: Any, event: str, payload: dict[str, Any]) -> None:
        self.frames.append({"event": event, "payload": payload})


async def check_agent(source: Path, tmp_path: Path) -> list[dict[str, Any]]:
    from openjiuwen.agent_teams.spawn.shared_resources import _db_instances
    from jiuwenswarm.extensions_v2 import bootstrap
    from jiuwenswarm.extensions_v2.management import ExtensionManager
    from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
    from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
    from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

    previous_databases = set(_db_instances)
    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "install", report_path=tmp_path / "report.yaml")
    push = Push()
    runtime = WebUIRuntime(bus=bus, transport=RenderTransport(push=push),
                         catalog=WebUICatalogService(manager.active_packages))
    manager.set_inactive_callback(runtime.drop_package)
    await manager.init(bus, host_event_types=lambda bus: bootstrap.register_event_types(bus, runtime),
                       host_handlers=lambda bus: bootstrap.register_handlers(bus, runtime))
    try:
        archive = example_archive(source, tmp_path / "agent.zip")
        reports = await manager.install([archive])
        assert reports and all(r.status == "SUCCESS" for r in reports), reports
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        for item in manifest.get("ui", {}).get("items", []):
            if item.get("type") != "web.slot.contribution" or item.get("kind") in {"menu", "section"}:
                continue
            target = RenderTarget("demo-session", item["slot"], manifest["package"]["id"],
                                  item.get("instanceKey", item.get("id", "")),
                                  scope=item.get("scope", "session"))
            await runtime.dispatch(UiActionInput("ui.ready", target), user_id="demo-user", connection_id="demo-client")
        assert push.frames, "No business frame was rendered"
        assert not any("render_error" in str(f.get("event", "")) for f in push.frames), push.frames
        return push.frames
    finally:
        await manager.shutdown()
        for key in set(_db_instances) - previous_databases:
            await _db_instances.pop(key).close()


async def check_gateway(source: Path, tmp_path: Path) -> list[dict[str, Any]]:
    from agent_gateway.extensions_v2.config import GatewayExtensionConfig
    from agent_gateway.extensions_v2.runtime import GatewayRuntime

    channel = Push()
    runtime = GatewayRuntime(channel=channel, config=GatewayExtensionConfig(
        install_dir=tmp_path / "install", data_dir=tmp_path / "data",
        recovery_data_dir=tmp_path / "recovery", report_path=tmp_path / "report.yaml"))
    await runtime.start()
    try:
        archive = example_archive(source, tmp_path / "gateway.zip")
        reports = await runtime.packages.install([archive])
        assert reports and all(r.status == "SUCCESS" for r in reports), reports
        await runtime.browser.connect(channel.ws, "demo-client")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        context = SessionContext("gateway:demo-client", None, {"connection_id": "demo-client", "channel_id": "web"})
        from agent_gateway.extensions_v2.models import GatewayRequestInput
        if manifest["package"]["id"] == "webui.login-demo":
            response = await runtime.bus.trigger(Event("gateway.request", GatewayRequestInput(
                method="session.list", connection_id="demo-client", request_context={"headers": {}})), context)
            assert response.error["code"] == "AUTH_REQUIRED"
        if manifest["package"]["id"] == "sample.harness-workbench.gateway":
            params = {"event": "ui.select", "target": {"package_id": "sample.harness-workbench", "scope": "application"},
                      "payload": {"action_id": "harness.next-task-mode", "data": {"value": "codex"}}}
            await runtime.bus.trigger(Event("gateway.request", GatewayRequestInput(
                method="webui.dispatch", params=params, connection_id="demo-client")), context)
            await runtime.bus.trigger(Event("gateway.request", GatewayRequestInput(
                method="session.create", params={"session_id": "new-session"}, connection_id="demo-client")), context)
            rewritten = await runtime.bus.trigger(Event("gateway.request", GatewayRequestInput(
                method="chat.send", params={"session_id": "new-session", "message": "hello"}, connection_id="demo-client")), context)
            assert rewritten.params["agent_template_name"] == "harness-codex-expert"
        for item in manifest.get("ui", {}).get("items", []):
            if item.get("type") != "web.slot.contribution" or item.get("kind") in {"menu", "section"}:
                continue
            request = SimpleNamespace(connection_id="demo-client", request_context={}, params={
                "event": "ui.ready", "target": {"scope": "application", "slot_id": item["slot"],
                "package_id": manifest["package"]["id"], "instance_key": item.get("instanceKey", item.get("id", ""))},
                "payload": {}})
            await runtime.browser.dispatch(request, context)
        frames = [frame for frame in channel.frames if frame["event"] == "webui.render"]
        assert frames, channel.frames
        assert not any(frame["event"] == "webui.render_error" for frame in channel.frames), channel.frames
        return frames
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("folder", [
    "application-forms", "context-observer", "login",
    "team-collaboration", "token-weather", "theme",
])
async def test_demo_loads_and_renders(folder: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if folder == "login":
        monkeypatch.setenv("JIUWENSWARM_HOST_VERSION", "0.2.10")
    source = BASE / folder
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["package"]["domain"] == "gateway":
        await check_gateway(source, tmp_path)
    else:
        await check_agent(source, tmp_path)


async def test_bundle_members_load_and_render(tmp_path: Path) -> None:
    from openjiuwen.core.extension.bundle import read_bundle_spec
    from openjiuwen.core.extension.package_manager import PackageManager
    source = BASE / "harness-workbench"
    archive = example_archive(source, tmp_path / "bundle.zip")
    with PackageManager(tmp_path / "unpack").open_source(archive) as opened:
        spec = read_bundle_spec(opened.root)
        assert {m.domain for m in spec.members} == {"gateway", "agentserver"}
        for member in spec.members:
            destination = tmp_path / member.domain
            destination.mkdir()
            if member.domain == "gateway":
                await check_gateway(member.root, destination)
            else:
                await check_agent(member.root, destination)


async def test_bundle_install_activates_both_real_hosts(tmp_path: Path) -> None:
    from agent_gateway.extensions_v2.config import GatewayExtensionConfig
    from agent_gateway.extensions_v2.runtime import GatewayRuntime
    from jiuwenswarm.extensions_v2 import bootstrap
    from jiuwenswarm.extensions_v2.management import ExtensionManager, containers
    from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
    from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
    from jiuwenswarm.server.runtime.marketplace.extension_install_state import ExtensionInstallState

    install = tmp_path / "shared"
    report = tmp_path / "report.yaml"
    bus = ExtensionEventBus()
    manager = ExtensionManager(install, report_path=report)
    webui = WebUIRuntime(bus=bus, transport=RenderTransport(push=Push()),
                        catalog=WebUICatalogService(manager.active_packages))
    manager.set_inactive_callback(webui.drop_package)
    await manager.init(bus, host_event_types=lambda bus: bootstrap.register_event_types(bus, webui),
                       host_handlers=lambda bus: bootstrap.register_handlers(bus, webui))
    gateway = GatewayRuntime(channel=Push(), config=GatewayExtensionConfig(
        install_dir=install, data_dir=tmp_path / "gateway-data", report_path=tmp_path / "gateway-report.yaml",
        recovery_data_dir=tmp_path / "recovery", poll_interval=0.05))
    try:
        await gateway.start()
        archive = example_archive(BASE / "harness-workbench", tmp_path / "bundle.zip")
        store = ExtensionInstallState.for_install_dir(install)
        # Model the Gateway's existing management sync, using its real loader.
        installation = asyncio.create_task(containers.install_sources(
            manager, store, [archive], origin="user", require_all_members=True,
        ))
        try:
            async with asyncio.timeout(15):
                while not installation.done():
                    await gateway.sync_container_members()
                    await asyncio.sleep(0.05)
                outcomes = await installation
        finally:
            if not installation.done():
                installation.cancel()
                await asyncio.gather(installation, return_exceptions=True)
        assert outcomes[0].overall_status == "enabled", outcomes
        assert {item.package_id for item in manager.active_packages()} == {"sample.harness-workbench"}
        assert {item.package_id for item in gateway.active_packages()} == {"sample.harness-workbench.gateway"}
    finally:
        await gateway.shutdown()
        await manager.shutdown()
