# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.openjiuwen_fs_rwlock_patch import (
    install_fs_rwlock_task_affinity_patch,
    reset_fs_rwlock_patch,
)


@pytest.fixture(autouse=True)
def _reset_patch():
    reset_fs_rwlock_patch()
    yield
    reset_fs_rwlock_patch()


@pytest.mark.asyncio
async def test_concurrent_readers_release_without_task_mismatch(tmp_path: Path) -> None:
    from openjiuwen.core.sys_operation.local._async_read_write_lock import (
        HybridAsyncReadWriteLock,
    )

    assert install_fs_rwlock_task_affinity_patch() is True
    assert getattr(
        HybridAsyncReadWriteLock.read,
        "jiuwenswarm_fs_rwlock_patched",
        False,
    )

    lock = HybridAsyncReadWriteLock(tmp_path / "shared.db", is_singleton=False)
    barrier = asyncio.Barrier(2)
    errors: list[BaseException] = []

    async def reader() -> None:
        try:
            async with lock.read(timeout=5.0):
                await barrier.wait()
                await asyncio.sleep(0.05)
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            errors.append(exc)

    await asyncio.gather(reader(), reader())
    assert errors == []
    await lock.close()


@pytest.mark.asyncio
async def test_write_then_read_after_patch(tmp_path: Path) -> None:
    from openjiuwen.core.sys_operation.local._async_read_write_lock import (
        HybridAsyncReadWriteLock,
    )

    install_fs_rwlock_task_affinity_patch()
    lock = HybridAsyncReadWriteLock(tmp_path / "rw.db", is_singleton=False)

    async with lock.write(timeout=5.0):
        pass
    async with lock.read(timeout=5.0):
        pass

    await lock.close()


def test_install_is_idempotent() -> None:
    assert install_fs_rwlock_task_affinity_patch() is True
    assert install_fs_rwlock_task_affinity_patch() is True


@pytest.mark.asyncio
async def test_cancelled_reader_keeps_other_readers_hold(tmp_path: Path) -> None:
    from openjiuwen.core.sys_operation.local._async_read_write_lock import HybridAsyncReadWriteLock

    install_fs_rwlock_task_affinity_patch()
    lock = HybridAsyncReadWriteLock(tmp_path / "cancelled-reader.db", is_singleton=False)
    reader_ready = asyncio.Event()
    writer_entered = asyncio.Event()

    async def reader() -> None:
        async with lock.read(timeout=2):
            reader_ready.set()
            await asyncio.Event().wait()

    async def writer() -> None:
        async with lock.write(timeout=2):
            writer_entered.set()

    async with lock.read(timeout=2):
        task = asyncio.create_task(reader())
        await asyncio.wait_for(reader_ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pending_writer = asyncio.create_task(writer())
        await asyncio.sleep(0.02)
        assert not writer_entered.is_set()
    await asyncio.wait_for(pending_writer, 2)
    assert writer_entered.is_set()
    await lock.close()


@pytest.mark.asyncio
async def test_active_context_prevents_close_and_closed_lock_rejects_acquire(tmp_path: Path) -> None:
    from openjiuwen.core.sys_operation.local._async_read_write_lock import HybridAsyncReadWriteLock

    install_fs_rwlock_task_affinity_patch()
    lock = HybridAsyncReadWriteLock(tmp_path / "active-close.db", is_singleton=False)
    async with lock.read(timeout=2):
        with pytest.raises(RuntimeError, match="active file lock"):
            await lock.close()
    async with lock.write(timeout=2):
        with pytest.raises(RuntimeError, match="active file lock"):
            await lock.close()
    await lock.close()
    await lock.close()
    with pytest.raises(RuntimeError, match="closed file lock"):
        async with lock.read(timeout=2):
            pytest.fail("Closed lock admitted a reader")


@pytest.mark.asyncio
async def test_file_contention_timeout_preserves_owner_and_allows_retry(tmp_path: Path) -> None:
    from filelock import Timeout
    from openjiuwen.core.sys_operation.local._async_read_write_lock import HybridAsyncReadWriteLock

    install_fs_rwlock_task_affinity_patch()
    path = tmp_path / "contended.db"
    owner = HybridAsyncReadWriteLock(path, is_singleton=False)
    contender = HybridAsyncReadWriteLock(path, is_singleton=False)
    async with owner.write(timeout=2):
        with pytest.raises(Timeout):
            async with contender.read(timeout=0.05):
                pytest.fail("Contending reader acquired a write-locked file")
        with pytest.raises(Timeout):
            async with contender.write(timeout=0.05):
                pytest.fail("Contending writer acquired a write-locked file")
    async with contender.write(timeout=2):
        pass
    await owner.close()
    await contender.close()


def test_reset_restores_all_public_contexts() -> None:
    from openjiuwen.core.sys_operation.local._async_read_write_lock import HybridAsyncReadWriteLock

    original = (HybridAsyncReadWriteLock.read, HybridAsyncReadWriteLock.write, HybridAsyncReadWriteLock.close)
    install_fs_rwlock_task_affinity_patch()
    reset_fs_rwlock_patch()
    assert (HybridAsyncReadWriteLock.read, HybridAsyncReadWriteLock.write, HybridAsyncReadWriteLock.close) == original


@pytest.mark.parametrize("missing", ["read", "write", "close", "file_lock"])
def test_incompatible_sdk_interface_is_skipped_without_partial_install(monkeypatch, missing):
    from openjiuwen.core.sys_operation.local import _async_read_write_lock as core_lock

    class IncompatibleLock:
        file_lock = property(lambda self: None)

        def read(self):
            pass

        def write(self):
            pass

        async def close(self):
            pass

    delattr(IncompatibleLock, missing)
    originals = dict(vars(IncompatibleLock))
    monkeypatch.setattr(core_lock, "HybridAsyncReadWriteLock", IncompatibleLock)
    assert install_fs_rwlock_task_affinity_patch() is False
    assert dict(vars(IncompatibleLock)) == originals
    monkeypatch.undo()
    assert install_fs_rwlock_task_affinity_patch() is True
