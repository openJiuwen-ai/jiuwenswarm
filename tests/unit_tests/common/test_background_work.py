# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hung optional IO must retain bounded capacity without owning shutdown."""

import asyncio
import threading
from contextvars import ContextVar

import pytest

from jiuwenswarm.common.background_work import BackgroundWorkBusy, BoundedBackgroundWork


@pytest.mark.asyncio
async def test_timed_out_and_cancelled_waiters_cannot_allocate_more_threads():
    work = BoundedBackgroundWork(max_workers=1, name="test-optional-read")
    entered = threading.Event()
    release = threading.Event()

    def read():
        entered.set()
        release.wait(5)
        return threading.current_thread().daemon

    future = work.submit(read)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        wrapped = asyncio.wrap_future(future)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(wrapped), 0.01)
        assert not future.cancel(), "a running read must keep its capacity slot"
        for _ in range(100):
            with pytest.raises(BackgroundWorkBusy):
                work.submit(lambda: None)
    finally:
        release.set()
    assert await wrapped is True
    assert await asyncio.wrap_future(work.submit(lambda: "recovered")) == "recovered"


def test_context_and_failure_do_not_leak_a_capacity_slot():
    work = BoundedBackgroundWork(max_workers=1, name="test-context-read")
    context = ContextVar("optional-read-context", default="missing")
    token = context.set("session-one")
    try:
        assert work.submit(context.get).result(1) == "session-one"
    finally:
        context.reset(token)

    def fail():
        raise OSError("read failed")

    with pytest.raises(OSError, match="read failed"):
        work.submit(fail).result(1)
    assert work.submit(lambda: 42).result(1) == 42


@pytest.mark.parametrize("stage", ["construct", "start"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_thread_creation_failure_is_busy_and_releases_capacity(monkeypatch, stage, error_type):
    work = BoundedBackgroundWork(max_workers=1, name="test-thread-failure")
    error = error_type("cannot start another thread")

    def fail(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        if stage == "construct":
            patch.setattr(threading, "Thread", fail)
        else:
            patch.setattr(threading.Thread, "start", fail)
        with pytest.raises(BackgroundWorkBusy) as caught:
            work.submit(lambda: pytest.fail("unstarted callback ran"))
        assert caught.value.__cause__ is error
    assert work.submit(lambda: "recovered").result(1) == "recovered"


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_thread_start_interrupt_propagates_and_releases_capacity(monkeypatch, error_type):
    work = BoundedBackgroundWork(max_workers=1, name="test-thread-interrupt")

    def interrupt(*args, **kwargs):
        raise error_type()

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", interrupt)
        with pytest.raises(error_type):
            work.submit(lambda: None)
    assert work.submit(lambda: "recovered").result(1) == "recovered"
