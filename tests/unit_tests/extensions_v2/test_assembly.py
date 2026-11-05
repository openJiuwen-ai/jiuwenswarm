from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

import jiuwenswarm.extensions_v2.assembly as assembly
from .management.support import write_package


class FakeServer:
    def __init__(self) -> None:
        self.messages = []

    async def send_push(self, message) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
async def test_install_extensions_only_activates_installed_packages_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
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
    finally:
        await assembly.shutdown_extensions()

    assert assembly.get_extension_manager() is None


@pytest.mark.asyncio
async def test_empty_install_store_does_not_copy_or_load_examples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
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
async def test_assembly_failure_keeps_the_server_usable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)

    def fail() -> Path:
        raise RuntimeError("bad install dir")

    monkeypatch.setattr(assembly, "resolve_install_dir", fail)
    assert await assembly.install_extensions(FakeServer()) is None
