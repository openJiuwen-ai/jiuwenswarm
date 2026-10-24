# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.extension import ActivationReport
from openjiuwen.core.events import ExtensionEventBus
from openjiuwen.core.events import Event, EventHandlerOps, EventHandlerOpsBuilder, EventNext, HandlerLevel
from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.management import context_for

from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

from .support import make_bus, write_package


def _report(operation: str) -> ActivationReport:
    return ActivationReport("test-plan", "test.pkg", operation, "SUCCESS", 1, 2)


async def test_writes_wait_for_install_but_queries_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(make_bus())
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_install(paths: list[Path]) -> tuple[ActivationReport, ...]:
        entered.set()
        await release.wait()
        return ()

    monkeypatch.setattr(manager.registry, "install", blocking_install)
    reload = AsyncMock(return_value=_report("reload"))
    uninstall = AsyncMock(return_value=_report("uninstall"))
    monkeypatch.setattr(manager.registry, "reload", reload)
    monkeypatch.setattr(manager.registry, "uninstall", uninstall)
    first = asyncio.create_task(manager.install([]))
    await entered.wait()
    second = asyncio.create_task(manager.reload("test.pkg"))
    third = asyncio.create_task(manager.uninstall("test.pkg"))
    try:
        await asyncio.sleep(0)
        reload.assert_not_awaited()
        uninstall.assert_not_awaited()
        assert (await asyncio.wait_for(manager.query(), 2)).active == ()
    finally:
        release.set()
        await asyncio.gather(first, second, third)
    reload.assert_awaited_once()
    uninstall.assert_awaited_once()


async def test_cancelled_waiter_never_calls_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(make_bus())
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_install(paths: list[Path]) -> tuple[ActivationReport, ...]:
        entered.set()
        await release.wait()
        return ()

    monkeypatch.setattr(manager.registry, "install", blocking_install)
    reload = AsyncMock(return_value=_report("reload"))
    monkeypatch.setattr(manager.registry, "reload", reload)
    first = asyncio.create_task(manager.install([]))
    await entered.wait()
    waiter = asyncio.create_task(manager.reload("cancelled.pkg"))
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
        reload.assert_not_awaited()
    finally:
        release.set()
        await first
    await manager.reload("test.pkg")
    reload.assert_awaited_once_with("test.pkg")


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_writer_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(make_bus())
    error = asyncio.CancelledError if cancel else RuntimeError
    monkeypatch.setattr(manager.registry, "install", AsyncMock(side_effect=error))
    monkeypatch.setattr(manager.registry, "reload", AsyncMock(return_value=_report("reload")))

    with pytest.raises(error):
        await manager.install([])

    report = await asyncio.wait_for(manager.reload("test.pkg"), 2)
    assert report.status == "SUCCESS"


async def test_inactive_callback_can_query_and_install_again(tmp_path: Path) -> None:
    manager = ExtensionManager(tmp_path / "store")
    await manager.init(make_bus())
    await manager.install([write_package(tmp_path / "first")])
    replacement = write_package(tmp_path / "second", package_id="test.after")
    notified: list[str] = []

    async def replace_after_uninstall(package_id: str) -> None:
        assert (await manager.query()).active == ()
        notified.append(package_id)
        assert (await manager.install([replacement]))[0].status == "SUCCESS"

    manager.set_inactive_callback(replace_after_uninstall)
    assert (await asyncio.wait_for(manager.uninstall("test.pkg"), 2)).status == "SUCCESS"
    assert notified == ["test.pkg"]
    assert [record.package_id for record in manager.active_packages()] == ["test.after"]


async def test_concurrent_init_only_initializes_core_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ExtensionManager(tmp_path / "store")
    bus = make_bus()
    entered, release = asyncio.Event(), asyncio.Event()
    initialize = manager.registry.init

    async def delayed_init(
        event_bus: ExtensionEventBus,
        *,
        host_event_types: Callable[[ExtensionEventBus], None] | None = None,
        host_handlers: Callable[[ExtensionEventBus], None] | None = None,
    ) -> None:
        entered.set()
        await release.wait()
        await initialize(event_bus, host_event_types=host_event_types, host_handlers=host_handlers)

    spy = AsyncMock(side_effect=delayed_init)
    monkeypatch.setattr(manager.registry, "init", spy)
    first = asyncio.create_task(manager.init(bus))
    await entered.wait()
    second = asyncio.create_task(manager.init(bus))
    release.set()
    await asyncio.gather(first, second)
    spy.assert_awaited_once()


async def test_cancelled_initialization_releases_bound_core_context(tmp_path: Path) -> None:
    manager = ExtensionManager(tmp_path / "store")
    bus = make_bus()
    entered = asyncio.Event()

    async def blocked_build(
        event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        entered.set()
        await asyncio.Event().wait()
        return await next_handler(event)

    bus.register_event_handler("ops.build", blocked_build, {}, HandlerLevel.BUILTIN)
    task = asyncio.create_task(manager.init(bus))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.initialized
    with pytest.raises(ExtensionFailure):
        context_for(bus)
    await manager.init(make_bus())
    await manager.shutdown()
