"""Cancellation must not abandon a cross-process group lock in a worker thread."""

import asyncio
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime import group_service as gs


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
@pytest.mark.parametrize("stage", ["acquire", "release"])
async def test_group_lock_finishes_io_before_propagating_cancellation(
    tmp_path, monkeypatch, stage, cancel_count,
):
    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    resume = threading.Event()
    released = threading.Event()
    body_entered = False
    file_lock = gs.cross_process_file_lock

    def pause_io():
        loop.call_soon_threadsafe(started.set)
        assert resume.wait(5), "test did not release the lock worker"

    @contextmanager
    def guard(path):
        with file_lock(path, timeout=1):
            if stage == "acquire":
                pause_io()
            try:
                yield path
            finally:
                if stage == "release":
                    pause_io()
        released.set()

    monkeypatch.setattr(gs, "cross_process_file_lock", guard)

    async def writer():
        nonlocal body_entered
        async with gs.group_lock("cancelled-group"):
            body_entered = True

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "cancelled writer abandoned its lock I/O"
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert released.is_set()
        assert body_entered == (stage == "release")
        with file_lock(tmp_path / "group-locks" / "cancelled-group", timeout=0.1):
            pass
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(released.wait, 2)


@pytest.mark.asyncio
async def test_group_lock_acquisition_failure_does_not_enter_body(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")

    guard = MagicMock()
    guard.__enter__.side_effect = TimeoutError("lock unavailable")
    monkeypatch.setattr(gs, "cross_process_file_lock", lambda _path: guard)
    with pytest.raises(TimeoutError, match="lock unavailable"):
        async with gs.group_lock("unavailable-group"):
            pytest.fail("failed acquisition entered the critical section")
    guard.__exit__.assert_not_called()


@pytest.mark.asyncio
async def test_group_lock_serializes_writers(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    active = 0
    completed = []

    async def writer(index):
        nonlocal active
        async with gs.group_lock("shared-group"):
            active += 1
            assert active == 1
            await asyncio.sleep(0)
            completed.append(index)
            active -= 1

    await asyncio.wait_for(asyncio.gather(*(writer(index) for index in range(5))), 3)
    assert sorted(completed) == list(range(5))


@pytest.mark.asyncio
async def test_group_lock_releases_real_file_lock_after_body_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    with pytest.raises(ValueError, match="body failed"):
        async with gs.group_lock("failed-group"):
            raise ValueError("body failed")
    with gs.cross_process_file_lock(tmp_path / "group-locks" / "failed-group", timeout=0.1):
        pass
