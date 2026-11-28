# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for slow-only periodic-task spans."""

from __future__ import annotations

import time

import pytest

from jiuwenswarm.runtime_trace import state
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings
from jiuwenswarm.runtime_trace.tasks import record_task


class _Recorder:
    def __init__(self) -> None:
        self.started = []
        self.ended = []

    def start(self, name, *, level="normal", mono_start=None, attributes=None):
        self.started.append((name, level, mono_start))
        return object()

    def end(self, handle, outcome, *, mono_end=None, attributes=None):
        self.ended.append((outcome, mono_end))


def _settings(enabled: bool = True, slow_ms: int = 1000) -> RuntimeTraceSettings:
    return RuntimeTraceSettings(
        enabled=enabled,
        slow_task_ms=slow_ms,
        retention_days=7,
        exporter="none",
    )


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    state.clear_state()


def test_fast_task_produces_no_span() -> None:
    recorder = _Recorder()
    state.set_state(recorder, _settings(slow_ms=1000))
    start = time.monotonic_ns()
    record_task("task.heartbeat_tick", start, start + 500_000_000)
    assert recorder.started == []


def test_threshold_is_inclusive() -> None:
    recorder = _Recorder()
    state.set_state(recorder, _settings(slow_ms=1000))
    start = time.monotonic_ns()
    record_task("task.heartbeat_tick", start, start + 1_000_000_000)
    assert recorder.started == []


def test_slow_task_records_slow_outcome() -> None:
    recorder = _Recorder()
    state.set_state(recorder, _settings(slow_ms=10))
    start = time.monotonic_ns()
    end = start + 50_000_000
    record_task("task.auto_harness_execute", start, end)
    assert recorder.started == [
        ("task.auto_harness_execute", "normal", start)
    ]
    assert recorder.ended == [("slow", end)]


def test_failed_task_records_error_even_when_fast() -> None:
    recorder = _Recorder()
    state.set_state(recorder, _settings(slow_ms=1000))
    start = time.monotonic_ns()
    end = start + 1_000_000
    record_task("task.heartbeat_tick", start, end, failed=True)
    assert recorder.started[0][0] == "task.heartbeat_tick"
    assert recorder.ended == [("error", end)]


def test_disabled_settings_produce_no_span() -> None:
    recorder = _Recorder()
    state.set_state(recorder, _settings(enabled=False, slow_ms=1))
    start = time.monotonic_ns()
    record_task("task.heartbeat_tick", start, start + 10_000_000, failed=True)
    assert recorder.started == []


def test_no_recorder_is_a_noop() -> None:
    state.clear_state()
    start = time.monotonic_ns()
    record_task("task.heartbeat_tick", start, start + 10_000_000, failed=True)
