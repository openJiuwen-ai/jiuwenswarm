from __future__ import annotations

import asyncio
from pathlib import Path
from shutil import copytree

import pytest

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput
from .management.support import write_package


class FakeServer:
    def __init__(self) -> None:
        self.messages = []

    async def send_push(self, message) -> bool:
        self.messages.append(message)
        return True


def _runner_extension_bus_accessors():
    try:
        from openjiuwen.core.runner import Runner
    except ImportError:
        return None, None
    return getattr(Runner, "get_extension_bus", None), getattr(Runner, "set_extension_bus", None)


def _reset_runner_extension_bus() -> None:
    _, set_bus = _runner_extension_bus_accessors()
    if set_bus is not None:
        set_bus(None)


@pytest.mark.asyncio
async def test_install_extensions_only_activates_installed_packages_once(tmp_path: Path, monkeypatch) -> None:
    _reset_runner_extension_bus()
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "report.yaml")
    store = tmp_path / "store"
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: store)
    source = write_package(tmp_path / "source", package_id="saved.pkg")
    copytree(source, store / "saved.pkg" / "1.0.0")

    first = await assembly.install_extensions(FakeServer())
    second = await assembly.install_extensions(FakeServer())
    try:
        assert first is not None
        assert first is second
        active = {record.package_id for record in first.manager.active_packages()}
        assert active == {"saved.pkg"}
        assert assembly.get_extension_manager() is first.manager
        assert assembly.get_webui_runtime() is first.webui
        get_bus, _ = _runner_extension_bus_accessors()
        if get_bus is not None:
            assert get_bus() is first.bus
    finally:
        await assembly.shutdown_extensions()

    assert assembly.get_extension_manager() is None
    get_bus, _ = _runner_extension_bus_accessors()
    if get_bus is not None:
        assert get_bus() is None


@pytest.mark.asyncio
async def test_empty_install_store_does_not_copy_or_load_examples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "report.yaml")
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    instance = await assembly.install_extensions(FakeServer())
    try:
        assert instance is not None
        assert instance.manager.active_packages() == ()
        assert (await instance.manager.query()).installed == ()
        assert not (tmp_path / "webui.token-weather").exists()
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_reload_clears_views_before_notifying_ready_clients(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "report.yaml")
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    server = FakeServer()
    instance = await assembly.install_extensions(server)
    assert instance is not None
    try:
        package = write_package(tmp_path / "source", package_id="reload.pkg")
        assert (await instance.manager.install([package]))[0].status == "SUCCESS"
        target = RenderTarget("session-1", "conversation.input.below", "reload.pkg", "panel")
        instance.webui.remember_ready_client("ws-1", "session-1")
        await instance.webui.render(UiRenderInput(target, root=RenderNode("Text", {"value": "old"})))
        server.messages.clear()

        report = await instance.manager.reload("reload.pkg")
        assert report.status == "SUCCESS"
        for _ in range(50):
            event_types = [message["payload"]["event_type"] for message in server.messages]
            if "extensions.v2.installed" in event_types:
                break
            await asyncio.sleep(0.01)

        event_types = [message["payload"]["event_type"] for message in server.messages]
        assert event_types.index("webui.render.clear") < event_types.index("extensions.v2.installed")
        changed = next(message for message in server.messages
                       if message["payload"]["event_type"] == "extensions.v2.installed")
        assert changed["metadata"] == {"ws_id": "ws-1"}
        assert changed["payload"]["package_ids"] == ["reload.pkg"]
        assert changed["payload"]["session_id"] == "session-1"
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_assembly_failure_keeps_the_server_usable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "report.yaml")

    def fail() -> Path:
        raise RuntimeError("bad install dir")

    monkeypatch.setattr(assembly, "resolve_install_dir", fail)
    assert await assembly.install_extensions(FakeServer()) is None
