# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for attach/shutdown/failure lifecycle semantics."""

from __future__ import annotations

import sqlite3
import time
import zlib
from pathlib import Path

import pytest

from jiuwenswarm.runtime_trace import lifecycle, state
from jiuwenswarm.runtime_trace.clock import ClockBase
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings


@pytest.fixture()
def trace_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")
    with lifecycle._lock:
        lifecycle._components = None
    state.clear_state()
    yield tmp_path
    if lifecycle._components is not None:
        try:
            lifecycle.shutdown_runtime_trace(
                deadline_mono=time.monotonic() + 0.2
            )
        except Exception:  # noqa: BLE001
            pass
        lifecycle._reset()


def _settings(enabled: bool = True) -> RuntimeTraceSettings:
    return RuntimeTraceSettings(
        enabled=enabled,
        slow_task_ms=1000,
        retention_days=7,
        exporter="none",
    )


def test_service_name_keeps_the_role_suffix() -> None:
    assert lifecycle._service_name_for("my-swarm", "front") == "my-swarm-front"
    assert lifecycle._service_name_for("my-swarm", "worker") == "my-swarm-worker"


def test_service_name_defaults_per_role() -> None:
    assert lifecycle._service_name_for("", "front") == "jiuwenswarm-agentserver-front"
    assert lifecycle._service_name_for("", "sidecar") == "jiuwenswarm-agentserver-sidecar"


def _database(workspace: Path) -> Path:
    matches = list((workspace / "runtime").glob("*/*.sqlite3"))
    assert len(matches) == 1, matches
    return matches[0]


def _rows(database: Path) -> list[tuple]:
    conn = sqlite3.connect(database)
    try:
        return list(
            conn.execute(
                "SELECT name, outcome, parent_span_id, raw_json FROM span_records "
                "ORDER BY start_ns"
            )
        )
    finally:
        conn.close()


def _raw_contains(row: tuple, needle: str) -> bool:
    return needle.encode() in zlib.decompress(row[3])


def _meta(database: Path) -> tuple:
    conn = sqlite3.connect(database)
    try:
        return conn.execute(
            "SELECT worker_instance, generation, closed_at FROM meta"
        ).fetchone()
    finally:
        conn.close()


def _started_at_and_root(database: Path) -> tuple[int, int]:
    conn = sqlite3.connect(database)
    try:
        started_at = conn.execute("SELECT started_at FROM meta").fetchone()[0]
        root_start = conn.execute(
            "SELECT start_ns FROM span_records WHERE name='startup'"
        ).fetchone()[0]
        return started_at, root_start
    finally:
        conn.close()


def test_attach_disabled_returns_none(trace_env: Path) -> None:
    assert lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(enabled=False)
    ) is None
    assert not (trace_env / "runtime").exists()


def test_attach_and_replay_worker_marks(trace_env: Path) -> None:
    clock = ClockBase()
    clock.mark("config")
    clock.mark("ipc_listening")
    clock.mark("preload_begin")
    clock.mark("preload")
    recorder = lifecycle.attach_runtime_trace(
        role="worker",
        settings=_settings(),
        clock=clock,
        worker_instance="instance",
        generation=2,
    )
    assert recorder is not None
    assert clock.replayed is True
    lifecycle.close_open_startup_root(outcome="success")
    lifecycle.start_shutdown_root()
    assert lifecycle.shutdown_runtime_trace(outcome="success") is True

    database = _database(trace_env)
    names = [row[0] for row in _rows(database)]
    assert names.count("startup.phase.config") == 1
    assert names.count("startup.phase.ipc_listening") == 1
    assert names.count("startup.phase.preload") == 1
    assert "startup" in names and "shutdown" in names
    assert _meta(database)[0] == "instance"
    assert _meta(database)[1] == 2
    assert _meta(database)[2] is not None


def test_replay_happens_once(trace_env: Path) -> None:
    clock = ClockBase()
    clock.mark("config")
    recorder = lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock
    )
    assert recorder is not None
    before = len(recorder._kinds._entries)  # noqa: SLF001 - test inspection
    lifecycle._replay_early_marks(recorder, clock)  # noqa: SLF001
    assert len(recorder._kinds._entries) == before
    lifecycle.shutdown_runtime_trace()


def test_close_open_startup_root_defaults_to_cancelled(trace_env: Path) -> None:
    recorder = lifecycle.attach_runtime_trace(
        role="front", settings=_settings()
    )
    assert recorder is not None
    lifecycle.close_open_startup_root()
    lifecycle.shutdown_runtime_trace()
    rows = _rows(_database(trace_env))
    startup = next(row for row in rows if row[0] == "startup")
    assert startup[1] == "cancelled"
    assert _raw_contains(startup, "cancel_requested")


def test_close_open_startup_root_ready_timeout(trace_env: Path) -> None:
    recorder = lifecycle.attach_runtime_trace(
        role="front", settings=_settings()
    )
    assert recorder is not None
    recorder.startup_handle.mono_start -= 301 * 1_000_000_000
    lifecycle.close_open_startup_root()
    lifecycle.shutdown_runtime_trace()
    startup = next(
        row for row in _rows(_database(trace_env)) if row[0] == "startup"
    )
    assert startup[1] == "error"
    assert _raw_contains(startup, "ready_timeout")


def test_shutdown_is_idempotent(trace_env: Path) -> None:
    lifecycle.attach_runtime_trace(role="front", settings=_settings())
    lifecycle.close_open_startup_root(outcome="success")
    lifecycle.start_shutdown_root()
    assert lifecycle.shutdown_runtime_trace() is True
    assert lifecycle.shutdown_runtime_trace() is True
    assert lifecycle.shutdown_runtime_trace(
        deadline_mono=time.monotonic() - 1
    ) is True


def test_meta_started_at_matches_startup_root(trace_env: Path) -> None:
    startup_start = time.monotonic_ns() - 2_000_000_000
    lifecycle.attach_runtime_trace(
        role="front",
        settings=_settings(),
        startup_start_mono_ns=startup_start,
    )
    lifecycle.close_open_startup_root(outcome="success")
    lifecycle.shutdown_runtime_trace()
    started_at, root_start = _started_at_and_root(_database(trace_env))
    assert started_at == root_start


def test_start_shutdown_root_without_recorder_is_noop() -> None:
    state.clear_state()
    lifecycle.start_shutdown_root()
    lifecycle.close_open_startup_root()
    assert lifecycle.shutdown_runtime_trace() is True


@pytest.mark.asyncio
async def test_spawn_injects_run_trace_without_traceparent(
    trace_env: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.server.supervisor import worker_process

    monkeypatch.delenv("TRACEPARENT", raising=False)
    recorder = lifecycle.attach_runtime_trace(
        role="front", settings=_settings()
    )
    assert recorder is not None
    startup_context = recorder.startup_handle.span.get_span_context()

    captured: dict = {}
    proc = SimpleNamespace(pid=4321, returncode=None, poll=lambda: None)

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(worker_process.subprocess, "Popen", fake_popen)
    process = worker_process.WorkerProcess(
        str(trace_env / "worker.sock"),
        host="127.0.0.1",
        port=18092,
        recorder=recorder,
    )
    await process.spawn()

    traceparent = captured["env"]["TRACEPARENT"]
    trace_id = int(traceparent.split("-")[1], 16)
    assert trace_id == startup_context.trace_id
    lifecycle.close_open_startup_root(outcome="success")
    lifecycle.shutdown_runtime_trace()


def test_shutdown_budget_is_short_and_bounded(trace_env: Path) -> None:
    lifecycle.attach_runtime_trace(role="front", settings=_settings())
    started = time.monotonic()
    # An exhausted deadline gives up immediately; the writer may still be
    # finishing its stop request, so the drain result itself is not asserted.
    lifecycle.shutdown_runtime_trace(deadline_mono=time.monotonic() - 1)
    assert time.monotonic() - started < 0.5


def test_failure_attach_with_existing_recorder(trace_env: Path) -> None:
    clock = ClockBase()
    recorder = lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock, worker_instance="i"
    )
    assert recorder is not None

    lifecycle.attach_runtime_trace_on_failure(
        settings=_settings(),
        clock=clock,
        worker_instance="i",
        generation=1,
        reason_code="runtime_load_failed",
    )
    assert lifecycle.current_recorder() is None
    database = _database(trace_env)
    startup = next(
        row for row in _rows(database) if row[0] == "startup"
    )
    assert startup[1] == "error"
    assert _raw_contains(startup, "runtime_load_failed")
    # One database only: failure attach never assembles a second run.
    assert len(list((trace_env / "runtime").glob("*/*.sqlite3"))) == 1
    assert lifecycle.shutdown_runtime_trace() is True


def test_failure_attach_without_recorder(trace_env: Path) -> None:
    clock = ClockBase()
    clock.mark("config")
    lifecycle.attach_runtime_trace_on_failure(
        settings=_settings(),
        clock=clock,
        worker_instance="i",
        generation=2,
        reason_code="runtime_load_failed",
    )
    assert lifecycle.current_recorder() is None
    assert clock.replayed is True
    database = _database(trace_env)
    rows = _rows(database)
    startup = next(row for row in rows if row[0] == "startup")
    assert startup[1] == "error"
    assert any(row[0] == "startup.phase.config" for row in rows)


def test_failure_attach_disabled_is_noop(trace_env: Path) -> None:
    clock = ClockBase()
    lifecycle.attach_runtime_trace_on_failure(
        settings=_settings(enabled=False),
        clock=clock,
        worker_instance=None,
        generation=None,
        reason_code="runtime_load_failed",
    )
    assert not (trace_env / "runtime").exists()


def test_failure_attach_without_settings_or_clock_is_noop(trace_env: Path) -> None:
    lifecycle.attach_runtime_trace_on_failure(
        settings=None,
        clock=None,
        worker_instance=None,
        generation=None,
        reason_code="runtime_load_failed",
    )
    assert not (trace_env / "runtime").exists()
