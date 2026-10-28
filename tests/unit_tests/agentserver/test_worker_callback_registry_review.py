# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regressions for completion races and cancellation during Worker cleanup."""

import asyncio
from concurrent.futures import Future, wait
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.worker import callback_registry, lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, ValueError("registry failed"), TimeoutError("registry timeout")])
async def test_completed_sync_call_returns_its_outcome_after_poll_timeout(monkeypatch, error):
    class CompletedAfterPollFuture(Future):
        def result(self, timeout=None):
            if timeout is not None:
                done, _pending = wait({self}, timeout=2)
                assert self in done
                # Model completion after Future.result's timed wait expired,
                # before the caller checks Future.done().
                raise TimeoutError("poll interval expired")
            return super().result(timeout=timeout)

    calls = []

    def register():
        calls.append(True)
        if error is not None:
            raise error
        return "registered"

    monkeypatch.setattr(callback_registry, "Future", CompletedAfterPollFuture)
    framework = SimpleNamespace(register_sync=register)
    owner = callback_registry.bind_callback_registry_owner(framework)

    def invoke():
        try:
            return True, framework.register_sync()
        except Exception as exc:
            return False, exc

    try:
        succeeded, result = await asyncio.wait_for(asyncio.to_thread(invoke), 3)
        if error is None:
            assert succeeded and result == "registered"
        else:
            assert not succeeded and result is error
        assert calls == [True]
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_sync_method_timeout_is_propagated_without_repeated_result_reads(monkeypatch):
    reads = []
    error = TimeoutError("SDK method timed out")

    class CountedFuture(Future):
        def result(self, timeout=None):
            reads.append(timeout)
            # Keep a faulty retry-on-TimeoutError implementation from leaving
            # an infinite loop in the executor thread during this regression.
            assert len(reads) <= 3
            return super().result(timeout=timeout)

    def register():
        raise error

    monkeypatch.setattr(callback_registry, "Future", CountedFuture)
    framework = SimpleNamespace(register_sync=register)
    owner = callback_registry.bind_callback_registry_owner(framework)

    def invoke():
        try:
            framework.register_sync()
        except Exception as exc:
            return exc
        return None

    try:
        result = await asyncio.wait_for(asyncio.to_thread(invoke), 3)
        assert result is error
    finally:
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [OSError("startup failed"), asyncio.CancelledError("startup cancelled")])
async def test_cleanup_cancellation_preserves_original_startup_error(
    monkeypatch, worker_callback_framework, original,
):
    async def stop():
        raise asyncio.CancelledError("cleanup cancelled")

    async def start(_host, _port, _signals, *, runtime, on_constructed, startup_t0):
        runtime.server = SimpleNamespace(stop=stop)
        raise original

    monkeypatch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
    monkeypatch.setattr(lifecycle, "_start_worker_runtime", start)
    with pytest.raises(type(original)) as caught:
        await lifecycle.start_worker_runtime("127.0.0.1", 18000, SimpleNamespace())
    assert caught.value is original
    with pytest.raises(RuntimeError, match="owner is closed"):
        worker_callback_framework.has_subscribers("event")


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_failed_startup_cleanup(
    monkeypatch, worker_callback_framework,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()
    original = OSError("startup failed")

    async def stop():
        entered.set()
        await release.wait()
        stopped.set()

    async def start(_host, _port, _signals, *, runtime, on_constructed, startup_t0):
        runtime.server = SimpleNamespace(stop=stop)
        raise original

    monkeypatch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
    monkeypatch.setattr(lifecycle, "_start_worker_runtime", start)
    task = asyncio.create_task(lifecycle.start_worker_runtime("127.0.0.1", 18000, SimpleNamespace()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        task.cancel("third cancellation")
        release.set()
        with pytest.raises(OSError) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value is original
        assert stopped.is_set()
        with pytest.raises(RuntimeError, match="owner is closed"):
            worker_callback_framework.has_subscribers("event")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_registry_close_finishes_after_shutdown_task_is_cancelled(worker_callback_framework):
    framework = worker_callback_framework
    owner = callback_registry.bind_callback_registry_owner(framework)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    closing_tasks = []
    original_close = owner.aclose

    async def close():
        closing_tasks.append(asyncio.current_task())
        entered.set()
        await release.wait()
        await original_close()
        finished.set()

    async def teardown():
        pass

    owner.aclose = close
    runtime = lifecycle.WorkerRuntime(
        server=SimpleNamespace(), stop_event=asyncio.Event(), callback_registry_owner=owner,
    )
    runtime._shutdown_owned_resources = teardown
    task = asyncio.create_task(runtime.shutdown())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="owner is closed"):
            framework.has_subscribers("event")
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        with pytest.raises(RuntimeError, match="owner is closed"):
            framework.has_subscribers("event")
    finally:
        release.set()
        await asyncio.gather(task, *closing_tasks, return_exceptions=True)
        owner.aclose = original_close
        await owner.aclose()


@pytest.mark.asyncio
async def test_stuck_partial_server_cleanup_is_bounded_and_releases_waiters(
    monkeypatch, worker_callback_framework,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    registering = asyncio.Event()
    stop_tasks = []
    waiting_calls = []
    original = OSError("startup failed")

    async def register(_event, _callback):
        registering.set()
        await asyncio.Event().wait()

    async def stop():
        stop_tasks.append(asyncio.current_task())
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    async def start(_host, _port, _signals, *, runtime, on_constructed, startup_t0):
        runtime.server = SimpleNamespace(stop=stop)
        waiting_calls.append(asyncio.create_task(asyncio.to_thread(
            lambda: asyncio.run(worker_callback_framework.register("event", lambda: None)),
        )))
        await registering.wait()
        raise original

    monkeypatch.setattr(worker_callback_framework, "register", register)
    monkeypatch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
    monkeypatch.setattr(lifecycle, "_start_worker_runtime", start)
    monkeypatch.setattr(lifecycle, "_STARTUP_FAILURE_CLEANUP_TIMEOUT", 0.02, raising=False)
    task = asyncio.create_task(lifecycle.start_worker_runtime("127.0.0.1", 18000, SimpleNamespace()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert task in done, "startup failure must not wait forever for a partial Server"
        with pytest.raises(OSError) as caught:
            await task
        assert caught.value is original
        with pytest.raises(RuntimeError, match="owner is closed"):
            worker_callback_framework.has_subscribers("event")
        result = await asyncio.wait_for(asyncio.gather(*waiting_calls, return_exceptions=True), 2)
        assert len(result) == 1 and isinstance(result[0], asyncio.CancelledError)
    finally:
        release.set()
        await asyncio.gather(task, *stop_tasks, return_exceptions=True)
        await asyncio.gather(*waiting_calls, return_exceptions=True)
