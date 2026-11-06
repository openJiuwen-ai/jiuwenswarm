# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from openjiuwen.core.events import Event

from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

from .support import (
    CORE_LEVEL_EXTENSION,
    METADATA_EXTENSION,
    PATTERN_EXTENSION,
    TWO_HANDLERS_EXTENSION,
    make_bus,
    write_package,
)


def _code(report) -> str | None:
    return report.error.code if report.error is not None else None


async def test_install_publishes_handler_with_verified_package_metadata(
    tmp_path: Path,
) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    await manager.install([source])

    result = await bus.trigger(
        Event("test.event", {"package_id": "spoofed", "package_version": "9.9.9"}),
    )

    assert result == (
        "test.pkg",
        "1.0.0",
        str((manager.install_dir / "test.pkg" / "1.0.0").resolve()),
    )


async def test_handler_is_invisible_before_install(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)

    assert await bus.trigger(Event("test.event", {})) == ("core", {})

    await manager.install([
        write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    ])

    assert (await bus.trigger(Event("test.event", {})))[0] == "test.pkg"


async def test_match_pattern_filters_package_handler(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "source", module_source=PATTERN_EXTENSION)])

    matched = await bus.trigger(Event("test.event", {"target": "a"}))
    unmatched = await bus.trigger(Event("test.event", {"target": "b"}))

    assert matched == ("user", {"target": "a"})
    assert unmatched == ("core", {"target": "b"})


async def test_core_level_registration_is_rejected(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source", module_source=CORE_LEVEL_EXTENSION)

    report = (await manager.install([source]))[0]

    assert report.status == "FAILED"
    assert _code(report) == "EXTENSION_HANDLER_LEVEL_FORBIDDEN"
    assert not (manager.install_dir / "test.pkg").exists()
    assert await bus.trigger(Event("test.event", {})) == ("core", {})


async def test_publish_failure_rolls_back_earlier_handler(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source", module_source=TWO_HANDLERS_EXTENSION)

    report = (await manager.install([source]))[0]

    assert report.status == "FAILED"
    assert _code(report) == "EXTENSION_LOAD_FAILED"
    assert not (manager.install_dir / "test.pkg").exists()
    assert await bus.trigger(Event("test.event", {})) == ("core", {})


async def test_uninstall_removes_future_handler_visibility(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    await manager.install([
        write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    ])
    assert (await bus.trigger(Event("test.event", {})))[0] == "test.pkg"

    await manager.uninstall("test.pkg")

    assert await bus.trigger(Event("test.event", {})) == ("core", {})


async def test_runtime_inflight_count_blocks_uninstall(tmp_path: Path) -> None:
    bus = make_bus()
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    await manager.install([write_package(tmp_path / "source")])
    runtime = manager.registry.runtime

    runtime.begin_calls(("test.pkg",))
    try:
        report = await manager.uninstall("test.pkg")
    finally:
        runtime.end_calls(("test.pkg",))

    assert _code(report) == "EXTENSION_PACKAGE_BUSY"
    assert (manager.install_dir / "test.pkg" / "1.0.0").is_dir()
    assert (await manager.uninstall("test.pkg")).status == "SUCCESS"
