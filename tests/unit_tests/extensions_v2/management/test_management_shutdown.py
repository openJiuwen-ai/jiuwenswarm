# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.events import Event, ExtensionEventBus
from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.management import context_for

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

from .support import METADATA_EXTENSION, make_bus, write_package


async def test_shutdown_releases_dependents_before_dependencies_and_unbinds_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(bus)
    roots = [
        write_package(tmp_path / "a", package_id="test.a"),
        write_package(tmp_path / "b", package_id="test.b", requires=(("test.a", ">=1.0.0"),)),
        write_package(tmp_path / "c", package_id="test.c", requires=(("test.b", ">=1.0.0"),)),
    ]
    assert all(report.status == "SUCCESS" for report in await manager.install(roots))
    released: list[str] = []
    unpublish = manager.registry.runtime.unpublish

    def record_release(package_id: str, target: ExtensionEventBus) -> None:
        unpublish(package_id, target)
        released.append(package_id)

    monkeypatch.setattr(manager.registry.runtime, "unpublish", record_release)
    await manager.shutdown()

    assert released == ["test.c", "test.b", "test.a"]
    assert not manager.initialized
    assert all((manager.install_dir / name / "1.0.0").is_dir() for name in released)
    with pytest.raises(ExtensionFailure):
        context_for(bus)
    await manager.init(make_bus())
    assert all(report.status == "SUCCESS" for report in await manager.load_installed())
    await manager.shutdown()


async def test_busy_package_and_its_dependency_remain_usable_until_retry(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(bus)
    roots = [
        write_package(tmp_path / "dep", package_id="test.dep"),
        write_package(
            tmp_path / "child", package_id="test.child", requires=(("test.dep", ">=1.0.0"),),
            module_source=METADATA_EXTENSION,
        ),
        write_package(tmp_path / "free", package_id="test.free"),
    ]
    await manager.install(roots)
    notified: list[str] = []

    async def record_inactive(package_id: str) -> None:
        notified.append(package_id)

    manager.set_inactive_callback(record_inactive)
    runtime = manager.registry.runtime
    runtime.begin_calls(["test.child"])
    try:
        with pytest.raises(ExtensionFailure, match="shutdown could not release"):
            await manager.shutdown()
        assert manager.initialized
        assert {record.package_id for record in (await manager.query()).active} == {"test.child", "test.dep"}
        assert (await bus.trigger(Event("test.event", {})))[0] == "test.child"
        assert notified == ["test.free"]
        assert context_for(bus).runtime is runtime
    finally:
        runtime.end_calls(["test.child"])
    await manager.shutdown()
    assert notified == ["test.free", "test.child", "test.dep"]
    assert not manager.initialized
    assert (manager.install_dir / "test.child" / "1.0.0").is_dir()


async def test_release_exception_retains_context_and_does_not_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "source", module_source=METADATA_EXTENSION)])
    notify = AsyncMock()
    manager.set_inactive_callback(notify)
    runtime = manager.registry.runtime
    unpublish = runtime.unpublish

    def fail_release(package_id: str, target: ExtensionEventBus) -> None:
        raise RuntimeError("release failed")

    monkeypatch.setattr(runtime, "unpublish", fail_release)
    with pytest.raises(ExtensionFailure):
        await manager.shutdown()
    notify.assert_not_awaited()
    assert (await manager.query()).active[0].package_id == "test.pkg"
    assert (await bus.trigger(Event("test.event", {})))[0] == "test.pkg"
    monkeypatch.setattr(runtime, "unpublish", unpublish)
    await manager.shutdown()
    notify.assert_awaited_once_with("test.pkg")


async def test_empty_shutdown_is_idempotent_and_unbinds_context(tmp_path: Path) -> None:
    manager = ExtensionManager(tmp_path / "store")
    bus = make_bus()
    await manager.init(bus)
    await manager.shutdown()
    await manager.shutdown()
    assert not manager.initialized
    with pytest.raises(ExtensionFailure):
        context_for(bus)


async def test_assembly_retains_failed_shutdown_for_query_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    instance = await assembly.install_extensions()
    assert instance is not None
    manager = instance.manager
    await manager.install([write_package(tmp_path / "source")])
    runtime = manager.registry.runtime
    runtime.begin_calls(["test.pkg"])
    clear_views = AsyncMock(wraps=instance.webui.shutdown)
    monkeypatch.setattr(instance.webui, "shutdown", clear_views)
    try:
        with pytest.raises(ExtensionFailure):
            await assembly.shutdown_extensions()
        assert assembly.get_extension_manager() is manager
        assert (await manager.query()).active[0].package_id == "test.pkg"
        clear_views.assert_not_awaited()
    finally:
        runtime.end_calls(["test.pkg"])
        await assembly.shutdown_extensions()
    clear_views.assert_awaited_once()
    assert assembly.get_extension_manager() is None


async def test_assembly_can_retry_view_cleanup_after_packages_are_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    instance = await assembly.install_extensions()
    assert instance is not None
    clear_views = AsyncMock(side_effect=[RuntimeError("clear failed"), None])
    monkeypatch.setattr(instance.webui, "shutdown", clear_views)
    try:
        with pytest.raises(RuntimeError, match="clear failed"):
            await assembly.shutdown_extensions()
        assert assembly.get_extension_manager() is instance.manager
        assert not instance.manager.initialized
    finally:
        await assembly.shutdown_extensions()
    assert clear_views.await_count == 2
    assert assembly.get_extension_manager() is None
