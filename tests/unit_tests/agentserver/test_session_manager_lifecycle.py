# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for SessionManager processor lifecycle ownership."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from jiuwenswarm.server.runtime.session.session_manager import SessionManager


async def _wait_until_idle(manager: SessionManager, session_id: str) -> None:
    for _ in range(100):
        if manager.get_current_task(session_id) is None:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"session did not become idle: {session_id}")


def _assert_session_absent(manager: SessionManager, session_id: str) -> None:
    assert session_id not in manager._session_tasks
    assert session_id not in manager._session_priorities
    assert session_id not in manager._session_queues
    assert session_id not in manager._session_processors
    assert session_id not in manager._closing_tasks


@pytest.mark.asyncio
async def test_close_session_releases_idle_processor_state() -> None:
    manager = SessionManager()
    completed = asyncio.Event()

    async def quick_task() -> None:
        completed.set()

    await manager.submit_task("tui_closed", quick_task)
    await asyncio.wait_for(completed.wait(), timeout=1)
    await _wait_until_idle(manager, "tui_closed")
    processor = manager._session_processors["tui_closed"]

    try:
        assert manager.has_active_processor("tui_closed") is True
        assert await manager.close_session("tui_closed") is True
        _assert_session_absent(manager, "tui_closed")
        assert await manager.close_session("tui_closed") is False
    finally:
        if not processor.done():
            processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_processor_does_not_retain_completed_task_closure() -> None:
    class Payload:
        pass

    manager = SessionManager()
    completed = asyncio.Event()
    payload = Payload()
    payload_ref = weakref.ref(payload)

    async def task_with_payload(value: Payload = payload) -> None:
        assert value is not None
        completed.set()

    await manager.submit_task("tui_reusable", task_with_payload)
    await asyncio.wait_for(completed.wait(), timeout=1)
    await _wait_until_idle(manager, "tui_reusable")
    del task_with_payload
    del payload
    gc.collect()

    processor = manager._session_processors["tui_reusable"]
    try:
        assert payload_ref() is None
        assert manager.has_active_processor("tui_reusable") is True
    finally:
        processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_session_cancels_running_task() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def running_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await manager.submit_task("tui_running", running_task)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await manager.close_session("tui_running") is True
    assert cancelled.is_set()
    _assert_session_absent(manager, "tui_running")


@pytest.mark.asyncio
async def test_cancel_session_task_timeout_keeps_running_task_tracked() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    allow_exit = asyncio.Event()

    async def cancellation_resistant_task() -> None:
        started.set()
        while not allow_exit.is_set():
            try:
                await allow_exit.wait()
            except asyncio.CancelledError:
                cancelling.set()

    await manager.submit_task("tui_cancel_timeout", cancellation_resistant_task)
    await asyncio.wait_for(started.wait(), timeout=1)
    running_task = manager.get_current_task("tui_cancel_timeout")
    processor = manager._session_processors["tui_cancel_timeout"]
    cancel_call = asyncio.create_task(
        manager.cancel_session_task("tui_cancel_timeout", wait_timeout=0.01)
    )
    await asyncio.wait_for(cancelling.wait(), timeout=1)

    try:
        done, pending = await asyncio.wait({cancel_call}, timeout=1)
        assert done == {cancel_call}
        assert not pending
        assert manager.get_current_task("tui_cancel_timeout") is running_task
        assert await manager.close_session(
            "tui_cancel_timeout", wait_timeout=0.01
        ) is True
        allow_exit.set()
        done, pending = await asyncio.wait({processor}, timeout=1)
        assert done == {processor}
        assert not pending
        assert "tui_cancel_timeout" not in manager._closing_tasks
    finally:
        allow_exit.set()
        await asyncio.wait_for(cancel_call, timeout=1)
        if not processor.done():
            processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_session_cancels_active_submit_and_wait_caller() -> None:
    manager = SessionManager()
    started = asyncio.Event()

    async def running_task() -> None:
        started.set()
        await asyncio.Event().wait()

    submitter = asyncio.create_task(
        manager.submit_and_wait("tui_active_waiter", running_task)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await manager.close_session("tui_active_waiter") is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(submitter, timeout=1)


@pytest.mark.asyncio
async def test_close_session_cancels_queued_submit_and_wait_caller() -> None:
    manager = SessionManager()
    blocker_started = asyncio.Event()

    async def blocking_task() -> None:
        blocker_started.set()
        await asyncio.Event().wait()

    async def queued_task() -> None:
        raise AssertionError("terminal close must not run queued work")

    await manager.submit_task("tui_queued_waiter", blocking_task)
    await asyncio.wait_for(blocker_started.wait(), timeout=1)
    submitter = asyncio.create_task(
        manager.submit_and_wait("tui_queued_waiter", queued_task)
    )
    for _ in range(100):
        if manager._session_queues["tui_queued_waiter"].qsize() == 1:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("submit_and_wait task was not queued")

    assert await manager.close_session("tui_queued_waiter") is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(submitter, timeout=1)


@pytest.mark.asyncio
async def test_concurrent_close_waits_for_same_generation() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    allow_exit = asyncio.Event()

    async def running_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await allow_exit.wait()
            raise

    await manager.submit_task("tui_duplicate_close", running_task)
    await asyncio.wait_for(started.wait(), timeout=1)
    first_close = asyncio.create_task(manager.close_session("tui_duplicate_close"))
    await asyncio.wait_for(cancelling.wait(), timeout=1)
    second_close = asyncio.create_task(manager.close_session("tui_duplicate_close"))

    try:
        await asyncio.sleep(0)
        assert second_close.done() is False
    finally:
        allow_exit.set()
        results = await asyncio.gather(first_close, second_close)

    assert results == [True, True]
    _assert_session_absent(manager, "tui_duplicate_close")


@pytest.mark.asyncio
async def test_close_timeout_keeps_processor_tracked_until_exit() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    allow_exit = asyncio.Event()

    async def slow_cancel_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await allow_exit.wait()
            raise

    await manager.submit_task("tui_slow_close", slow_cancel_task)
    await asyncio.wait_for(started.wait(), timeout=1)
    processor = manager._session_processors["tui_slow_close"]

    try:
        assert await asyncio.wait_for(
            manager.close_session("tui_slow_close", wait_timeout=0.01),
            timeout=0.2,
        ) is True
        assert cancelling.is_set()
        assert processor.done() is False
        assert processor in manager._closing_tasks["tui_slow_close"]
    finally:
        allow_exit.set()
        if not processor.done():
            processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)

    for _ in range(100):
        if "tui_slow_close" not in manager._closing_tasks:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("closing processor ownership was not released")


@pytest.mark.asyncio
async def test_closing_old_generation_keeps_reconnected_processor() -> None:
    manager = SessionManager()
    old_started = asyncio.Event()
    old_cancelling = asyncio.Event()
    allow_old_exit = asyncio.Event()
    replacement_ran = asyncio.Event()

    async def old_task() -> None:
        old_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancelling.set()
            await allow_old_exit.wait()
            raise

    async def replacement_task() -> None:
        replacement_ran.set()

    await manager.submit_task("tui_reconnect", old_task)
    await asyncio.wait_for(old_started.wait(), timeout=1)
    close_task = asyncio.create_task(manager.close_session("tui_reconnect"))
    await asyncio.wait_for(old_cancelling.wait(), timeout=1)

    await manager.submit_task("tui_reconnect", replacement_task)
    await asyncio.wait_for(replacement_ran.wait(), timeout=1)
    allow_old_exit.set()
    assert await asyncio.wait_for(close_task, timeout=1) is True

    assert manager.has_active_processor("tui_reconnect") is True
    assert "tui_reconnect" in manager._session_queues
    await manager.close_session("tui_reconnect")
