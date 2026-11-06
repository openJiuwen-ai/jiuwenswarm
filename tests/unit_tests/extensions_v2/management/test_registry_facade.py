# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from openjiuwen.core.events import Event, EventHandlerOpsBuilder, EventNext, HandlerLevel
from openjiuwen.core.extension import ExtensionFailure, PackageManager
from openjiuwen.core.extension.manifest import load_manifest

from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

from .support import METADATA_EXTENSION, make_bus, write_package


def _code(report) -> str | None:
    return report.error.code if report.error is not None else None


async def test_init_registers_host_once_and_triggers_ops_build(tmp_path: Path) -> None:
    bus = make_bus()
    builds: list[int] = []

    async def build(event, ops, next_handler: EventNext) -> EventHandlerOpsBuilder:
        builder = await next_handler(event)
        builds.append(1)
        return builder

    bus.register_event_handler("ops.build", build, {}, HandlerLevel.BUILTIN)
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")

    await manager.init(bus)
    await manager.init(bus)

    assert manager.initialized
    assert builds == [1]


async def test_install_query_reload_and_uninstall(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source", module_source=METADATA_EXTENSION)

    report = (await manager.install([source]))[0]

    assert (report.status, report.package_id, report.current_generation) == (
        "SUCCESS",
        "test.pkg",
        1,
    )
    result = await manager.query()
    assert [(item.package_id, item.version, item.generation) for item in result.active] == [
        ("test.pkg", "1.0.0", 1)
    ]
    assert (manager.install_dir / "test.pkg" / "1.0.0").is_dir()

    reloaded = await manager.reload("test.pkg")

    assert (reloaded.status, reloaded.current_generation) == ("SUCCESS", 2)
    assert (await manager.query()).active[0].generation == 2

    removed = await manager.uninstall("test.pkg")

    assert removed.status == "SUCCESS"
    assert (await manager.query()).active == ()
    assert not (manager.install_dir / "test.pkg" / "1.0.0").exists()


async def test_invalid_manifest_is_rejected_without_copy(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(
        tmp_path / "source",
        module="../outside.py",
        write_module=False,
    )

    report = (await manager.install([source]))[0]

    assert report.status == "FAILED"
    assert _code(report) == "EXTENSION_MANIFEST_INVALID"
    assert not (manager.install_dir / "test.pkg").exists()


async def test_duplicate_package_id_rejects_whole_batch(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    first = write_package(tmp_path / "first")
    second = write_package(tmp_path / "second")

    reports = await manager.install([first, second])

    assert [report.status for report in reports] == ["FAILED", "FAILED"]
    assert {_code(report) for report in reports} == {"EXTENSION_DUPLICATE_PACKAGE_ID"}
    assert not (manager.install_dir / "test.pkg").exists()


async def test_bad_source_does_not_block_independent_package(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    valid = write_package(tmp_path / "valid")

    reports = await manager.install([tmp_path / "missing", valid])

    assert [report.status for report in reports] == ["FAILED", "SUCCESS"]
    assert _code(reports[0]) == "EXTENSION_SOURCE_INVALID"
    assert reports[1].package_id == "test.pkg"


async def test_missing_dependency_is_reported_without_copy(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(
        tmp_path / "source",
        package_id="test.app",
        requires=(("test.dep", ">=1.0.0"),),
    )

    report = (await manager.install([source]))[0]

    assert _code(report) == "EXTENSION_DEPENDENCY_NOT_MET"
    assert not (manager.install_dir / "test.app").exists()


async def test_package_cycle_is_reported_without_copy(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    first = write_package(
        tmp_path / "first",
        package_id="test.a",
        requires=(("test.b", ">=1.0.0"),),
    )
    second = write_package(
        tmp_path / "second",
        package_id="test.b",
        requires=(("test.a", ">=1.0.0"),),
    )

    reports = await manager.install([first, second])

    assert {_code(report) for report in reports} == {"EXTENSION_DEPENDENCY_CYCLE"}
    assert not (manager.install_dir / "test.a").exists()
    assert not (manager.install_dir / "test.b").exists()


async def test_existing_store_version_is_not_overwritten(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source")
    await manager.install([source])
    await manager.shutdown()

    restarted = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await restarted.init(make_bus())
    report = (await restarted.install([source]))[0]

    assert _code(report) == "EXTENSION_INSTALL_COPY_FAILED"
    assert (restarted.install_dir / "test.pkg" / "1.0.0").is_dir()


async def test_uninstall_rejects_dependent_package(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    dependency = write_package(tmp_path / "dependency", package_id="test.dep")
    application = write_package(
        tmp_path / "application",
        package_id="test.app",
        requires=(("test.dep", ">=1.0.0"),),
    )

    reports = await manager.install([application, dependency])

    assert {report.package_id: report.status for report in reports} == {
        "test.dep": "SUCCESS",
        "test.app": "SUCCESS",
    }
    blocked = await manager.uninstall("test.dep")

    assert _code(blocked) == "EXTENSION_PACKAGE_IN_USE"
    assert {item.package_id for item in (await manager.query()).active} == {
        "test.dep",
        "test.app",
    }

    await manager.uninstall("test.app")
    assert (await manager.uninstall("test.dep")).status == "SUCCESS"


async def test_failed_reload_keeps_old_active_instance(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "v1", module_source=METADATA_EXTENSION)])
    installed = manager.install_dir / "test.pkg" / "1.0.0" / "extension.py"
    installed.write_text('raise RuntimeError("boom")\n', encoding="utf-8")

    report = await manager.reload("test.pkg")

    assert report.status == "FAILED"
    assert _code(report) == "EXTENSION_LOAD_FAILED"
    record = (await manager.query()).active[0]
    assert (record.package_id, record.version, record.generation) == ("test.pkg", "1.0.0", 1)
    assert await bus.trigger(Event("test.event", {})) == (
        "test.pkg",
        "1.0.0",
        str((manager.install_dir / "test.pkg" / "1.0.0").resolve()),
    )


async def test_uninstall_inactive_installed_copy(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source")
    package_manager = PackageManager(manager.install_dir)
    package_manager.copy_into_store(source, load_manifest(source))

    report = await manager.uninstall("test.pkg")

    assert report.status == "SUCCESS"
    assert not (manager.install_dir / "test.pkg" / "1.0.0").exists()


async def test_shutdown_releases_handlers_and_preserves_copy(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "source", module_source=METADATA_EXTENSION)])
    assert await bus.trigger(Event("test.event", {})) == (
        "test.pkg",
        "1.0.0",
        str((manager.install_dir / "test.pkg" / "1.0.0").resolve()),
    )

    await manager.shutdown()

    assert await bus.trigger(Event("test.event", {})) == ("core", {})
    assert (manager.install_dir / "test.pkg" / "1.0.0").is_dir()
    with pytest.raises(ExtensionFailure):
        await manager.query()


async def test_load_installed_uses_highest_version(tmp_path: Path) -> None:
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    await manager.install([write_package(tmp_path / "v1", version="1.0.0")])
    await manager.shutdown()

    newer = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await newer.init(make_bus())
    await newer.install([write_package(tmp_path / "v2", version="2.0.0")])
    await newer.shutdown()

    restarted = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await restarted.init(make_bus())
    reports = await restarted.load_installed()

    assert [(report.package_id, report.current_generation) for report in reports] == [
        ("test.pkg", 1)
    ]
    record = (await restarted.query()).active[0]
    assert (record.package_id, record.version) == ("test.pkg", "2.0.0")


async def test_load_installed_rejects_before_init(tmp_path: Path) -> None:
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")

    with pytest.raises(ExtensionFailure):
        await manager.load_installed()


async def test_inactive_callback_runs_after_uninstall(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    inactive: list[str] = []

    async def record_inactive(package_id: str) -> None:
        inactive.append(package_id)

    manager.set_inactive_callback(record_inactive)
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "source")])

    await manager.uninstall("test.pkg")
    await manager.shutdown()

    assert inactive == ["test.pkg"]
