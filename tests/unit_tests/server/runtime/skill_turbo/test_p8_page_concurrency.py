# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P8 postprocess concurrency helpers — event-loop starvation mitigations."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen as ppt_page_gen
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    _P8_1_POSTPROCESS_CONCURRENCY,
    _run_postprocess,
)


def _reset_postprocess_sem() -> None:
    ppt_page_gen._postprocess_sem = None


def test_p8_1_postprocess_concurrency_constant() -> None:
    assert _P8_1_POSTPROCESS_CONCURRENCY == 8


@pytest.mark.asyncio
async def test_run_postprocess_caps_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ppt_page_gen, "_P8_1_POSTPROCESS_CONCURRENCY", 3)
    _reset_postprocess_sem()

    current = 0
    peak = 0
    lock = threading.Lock()

    def _job() -> int:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        try:
            started = time.monotonic()
            while time.monotonic() - started < 0.05:
                pass
            return 1
        finally:
            with lock:
                current -= 1

    results = await asyncio.gather(
        *[_run_postprocess(_job) for _ in range(12)]
    )
    assert results == [1] * 12
    assert peak <= 3


@pytest.mark.asyncio
async def test_run_postprocess_uses_default_limit() -> None:
    _reset_postprocess_sem()

    current = 0
    peak = 0
    lock = threading.Lock()

    def _job() -> int:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        try:
            started = time.monotonic()
            while time.monotonic() - started < 0.05:
                pass
            return 1
        finally:
            with lock:
                current -= 1

    job_count = _P8_1_POSTPROCESS_CONCURRENCY + 4
    results = await asyncio.gather(
        *[_run_postprocess(_job) for _ in range(job_count)]
    )
    assert results == [1] * job_count
    assert peak <= _P8_1_POSTPROCESS_CONCURRENCY
