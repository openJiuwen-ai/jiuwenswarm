# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests: designer ffmpeg work must not block the Worker event loop."""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

from jiuwenswarm.server.runtime.designer.ffmpeg_runner import (
    FfmpegCancelled,
    run_ffmpeg,
    run_off_loop,
)

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_run_ffmpeg_returns_completed_process() -> None:
    result = run_ffmpeg([sys.executable, "-c", "import sys; sys.stderr.write('ok')"], timeout=30)

    assert result.returncode == 0
    assert result.stderr == "ok"


def test_run_ffmpeg_timeout_kills_child() -> None:
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="timed out"):
        run_ffmpeg(_SLEEPER, timeout=0.5)

    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_event_loop_keeps_ticking_during_blocking_media_work() -> None:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    task = asyncio.create_task(ticker())
    try:
        await run_off_loop(time.sleep, 0.6)
    finally:
        task.cancel()

    assert ticks >= 5


@pytest.mark.asyncio
async def test_cancel_kills_running_ffmpeg_and_blocks_new_starts() -> None:
    outcome: dict[str, BaseException | None] = {}
    first_started = threading.Event()

    def media_job() -> None:
        first_started.set()
        try:
            run_ffmpeg(_SLEEPER, timeout=60)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            outcome["first"] = exc
        try:
            run_ffmpeg(_SLEEPER, timeout=60)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            outcome["second"] = exc

    task = asyncio.create_task(run_off_loop(media_job))
    await asyncio.to_thread(first_started.wait, 10)
    await asyncio.sleep(0.3)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 10
    while "second" not in outcome and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    assert isinstance(outcome.get("first"), FfmpegCancelled)
    assert isinstance(outcome.get("second"), FfmpegCancelled)
    assert time.monotonic() - started < 10


def test_run_ffmpeg_outside_off_loop_scope_is_unaffected_by_cancellation() -> None:
    result = run_ffmpeg([sys.executable, "-c", "pass"], timeout=30)

    assert result.returncode == 0
