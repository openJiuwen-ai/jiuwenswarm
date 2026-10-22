from __future__ import annotations

from pathlib import Path

import pytest

import jiuwenswarm.extensions_v2.assembly as assembly


class FakeServer:
    def __init__(self) -> None:
        self.messages = []

    async def send_push(self, message) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
async def test_install_extensions_activates_example_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)

    first = await assembly.install_extensions(FakeServer())
    second = await assembly.install_extensions(FakeServer())
    try:
        assert first is not None
        assert first is second
        active = {record.package_id for record in first.manager.active_packages()}
        assert assembly.EXAMPLE_PACKAGE_ID in active
        assert assembly.get_extension_manager() is first.manager
        assert assembly.get_webui_runtime() is first.webui
    finally:
        await assembly.shutdown_extensions()

    assert assembly.get_extension_manager() is None


@pytest.mark.asyncio
async def test_assembly_failure_keeps_the_server_usable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)

    def fail() -> Path:
        raise RuntimeError("bad install dir")

    monkeypatch.setattr(assembly, "resolve_install_dir", fail)
    assert await assembly.install_extensions(FakeServer()) is None
