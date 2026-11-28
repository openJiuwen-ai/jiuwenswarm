# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Worker trace wiring, failure attach, and shutdown stages."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import types
import zlib
from pathlib import Path

import pytest

from jiuwenswarm.runtime_trace import lifecycle, state
from jiuwenswarm.runtime_trace.clock import ClockBase
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings
from jiuwenswarm.server.worker import lifecycle as worker_lifecycle
from jiuwenswarm.server.worker import service as worker_service


def _settings(enabled: bool = True) -> RuntimeTraceSettings:
    return RuntimeTraceSettings(
        enabled=enabled,
        slow_task_ms=1000,
        retention_days=7,
        exporter="none",
    )


def _trace_state(clock: ClockBase | None = None, enabled: bool = True) -> dict:
    return {
        "settings": _settings(enabled),
        "clock": clock or ClockBase(),
        "instance": "instance",
        "generation": 1,
    }


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
    state.clear_state()


def _database(workspace: Path) -> Path:
    matches = list((workspace / "runtime").glob("worker/*.sqlite3"))
    assert len(matches) == 1, matches
    return matches[0]


def _rows(database: Path) -> list[tuple]:
    conn = sqlite3.connect(database)
    try:
        return list(
            conn.execute(
                "SELECT name, outcome, raw_json FROM span_records ORDER BY start_ns"
            )
        )
    finally:
        conn.close()


def _contains(row: tuple, needle: str) -> bool:
    return needle.encode() in zlib.decompress(row[2])


def test_trace_recorder_helper_is_inert_when_disabled() -> None:
    assert worker_lifecycle._trace_recorder(None) is None
    assert worker_lifecycle._trace_recorder(_trace_state(enabled=False)) is None
    assert worker_service._trace_recorder(None) is None
    assert worker_service._trace_recorder(_trace_state(enabled=False)) is None
    assert worker_service._trace_lifecycle(None) is None


def test_agent_ready_records_wait_before_closing_root(monkeypatch) -> None:
    from jiuwenswarm.server.worker import signals as worker_signals

    class _Handle:
        ended = False

    class _Recorder:
        def __init__(self) -> None:
            self.startup_handle = _Handle()
            self.last_stage_mono = 1
            self.calls = []

        def start(self, name, **kwargs):
            self.calls.append(("start", name, kwargs))
            return name

        def end(self, handle, outcome, **kwargs):
            self.calls.append(("end", handle, outcome, kwargs))

        def event(self, handle, name, attributes=None):
            self.calls.append(("event", name, {}))

    class _Holder:
        def remember(self, message) -> None:
            return None

        @property
        def conn(self):
            return None

    recorder = _Recorder()
    monkeypatch.setattr(worker_signals, "_trace_recorder", lambda: recorder)
    worker_signals.RuntimeSignalSink(_Holder()).agent_ready()

    started = [call[1] for call in recorder.calls if call[0] == "start"]
    assert "startup.post.agent_ready" in started
    ends = [call for call in recorder.calls if call[0] == "end"]
    assert ends[-1][1] is recorder.startup_handle
    assert ends[-1][2] == "success"


def test_stage_name_mapping_covers_runtime_stages() -> None:
    expected = {
        "runtime_imports_ready",
        "extension_manager_created",
        "extensions_loaded",
        "runtime_constructed",
        "runtime_started",
        "observability_installed",
        "proactive_engine_initialized",
        "ready",
    }
    assert expected <= set(worker_lifecycle._STAGE_SPAN_NAMES)


@pytest.mark.asyncio
async def test_load_runtime_failure_reuses_attached_recorder(
    trace_env: Path, monkeypatch
) -> None:
    clock = ClockBase()
    clock.mark("config")
    recorder = lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock, worker_instance="instance"
    )
    assert recorder is not None
    trace_state = _trace_state(clock)

    async def _boom(*args, **kwargs):
        raise RuntimeError("runner import failed")

    monkeypatch.setattr(worker_lifecycle, "start_worker_runtime", _boom)

    class _Signals:
        def __init__(self):
            self.failed_reasons = []

        def failed(self, reason: str) -> None:
            self.failed_reasons.append(reason)

    signals = _Signals()
    dispatcher = types.SimpleNamespace(bind_runtime=lambda server: None)
    with pytest.raises(RuntimeError):
        await worker_service._load_runtime(
            "127.0.0.1",
            1,
            signals,
            dispatcher,
            {"runtime": None},
            trace_state,
        )

    assert signals.failed_reasons == ["runner import failed"]
    assert lifecycle.current_recorder() is None
    database = _database(trace_env)
    startup = next(row for row in _rows(database) if row[0] == "startup")
    assert startup[1] == "error"
    assert _contains(startup, "runtime_load_failed")
    # Only one run database: the attached recorder was reused, not rebuilt.
    assert len(list((trace_env / "runtime").glob("worker/*.sqlite3"))) == 1
    assert lifecycle.shutdown_runtime_trace() is True


@pytest.mark.asyncio
async def test_load_runtime_failure_attaches_when_preload_failed(
    trace_env: Path, monkeypatch
) -> None:
    clock = ClockBase()
    clock.mark("config")
    clock.mark("preload_begin")
    clock.mark("preload")
    trace_state = _trace_state(clock)

    async def _boom(*args, **kwargs):
        raise RuntimeError("preload failed")

    monkeypatch.setattr(worker_lifecycle, "start_worker_runtime", _boom)

    class _Signals:
        def failed(self, reason: str) -> None:
            return None

    dispatcher = types.SimpleNamespace(bind_runtime=lambda server: None)
    with pytest.raises(RuntimeError):
        await worker_service._load_runtime(
            "127.0.0.1",
            1,
            _Signals(),
            dispatcher,
            {"runtime": None},
            trace_state,
        )

    assert clock.replayed is True
    assert lifecycle.current_recorder() is None
    rows = _rows(_database(trace_env))
    by_name = {row[0]: row for row in rows}
    assert by_name["startup"][1] == "error"
    assert _contains(by_name["startup"], "runtime_load_failed")
    assert "startup.phase.config" in by_name
    assert "startup.phase.preload" in by_name


@pytest.mark.asyncio
async def test_preload_failure_records_error_phase(
    trace_env: Path, monkeypatch
) -> None:
    clock = ClockBase()
    clock.mark("config")
    clock.mark("ipc_listening")
    trace_state = _trace_state(clock)

    def boom() -> None:
        raise RuntimeError("preload failed inside runtime")

    monkeypatch.setattr(worker_lifecycle, "_preload_runtime_backend", boom)

    class _Signals:
        def failed(self, reason: str) -> None:
            return None

    dispatcher = types.SimpleNamespace(bind_runtime=lambda server: None)
    with pytest.raises(RuntimeError):
        await worker_service._load_runtime(
            "127.0.0.1",
            1,
            _Signals(),
            dispatcher,
            {"runtime": None},
            trace_state,
        )

    rows = _rows(_database(trace_env))
    by_name = {row[0]: row for row in rows}
    preload = by_name["startup.phase.preload"]
    assert preload[1] == "error"
    assert _contains(preload, "runtime_load_failed")
    assert clock.failed_phase == "preload"


@pytest.mark.asyncio
async def test_cancelled_worker_drains_shutdown_trace(
    trace_env: Path, monkeypatch
) -> None:
    clock = ClockBase()
    lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock
    )
    trace_state = _trace_state(clock)

    class _Server:
        def __init__(self, socket_path: str) -> None:
            self.socket_path = socket_path

        async def start(self) -> None:
            return None

        async def accept(self):
            await asyncio.Event().wait()

        async def close(self) -> None:
            return None

    class _Dispatcher:
        shutdown_requested = False

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def drain(self, timeout):
            return True

    monkeypatch.setattr(worker_service, "IpcServer", _Server)
    monkeypatch.setattr(worker_service, "WorkerDispatcher", _Dispatcher)

    task = asyncio.create_task(
        worker_service.serve_worker(
            str(trace_env / "worker.sock"),
            host="127.0.0.1",
            port=1,
            trace_state=trace_state,
        )
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if task.done():
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    database = _database(trace_env)
    conn = sqlite3.connect(database)
    try:
        rows = dict(conn.execute("SELECT name, outcome FROM span_records"))
        closed = conn.execute(
            "SELECT closed_at IS NOT NULL FROM meta"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows["startup"] == "cancelled"
    assert rows["shutdown"] == "cancelled"
    assert rows["shutdown.step.dispatch_drain"] == "success"
    assert rows["shutdown.step.runtime"] == "success"
    assert rows["shutdown.step.ipc"] == "success"
    assert closed == 1


@pytest.mark.asyncio
async def test_close_worker_records_three_steps(trace_env: Path) -> None:
    clock = ClockBase()
    lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock
    )
    lifecycle.start_shutdown_root()
    trace_state = _trace_state(clock)

    class _Dispatcher:
        async def drain(self, timeout):
            return True

    class _Server:
        async def close(self):
            return None

    await worker_service._close_worker(
        _Dispatcher(), None, {"runtime": None}, _Server(), trace_state=trace_state
    )
    lifecycle.shutdown_runtime_trace(outcome="success")
    rows = _rows(_database(trace_env))
    by_name = {row[0]: row for row in rows}
    for step in (
        "shutdown.step.dispatch_drain",
        "shutdown.step.runtime",
        "shutdown.step.ipc",
    ):
        assert by_name[step][1] == "success"


@pytest.mark.asyncio
async def test_close_worker_force_exit_records_dispatch_stuck(
    trace_env: Path,
) -> None:
    clock = ClockBase()
    lifecycle.attach_runtime_trace(
        role="worker", settings=_settings(), clock=clock
    )
    lifecycle.start_shutdown_root()
    trace_state = _trace_state(clock)

    class _Dispatcher:
        async def drain(self, timeout):
            return False

    class _Server:
        async def close(self):
            raise AssertionError("must not run after a force exit")

    with pytest.raises(worker_service.WorkerForceExit):
        await worker_service._close_worker(
            _Dispatcher(),
            None,
            {"runtime": None},
            _Server(),
            trace_state=trace_state,
        )
    lifecycle.shutdown_runtime_trace(outcome="cancelled", reason_code="dispatch_stuck")
    rows = _rows(_database(trace_env))
    by_name = {row[0]: row for row in rows}
    assert by_name["shutdown.step.dispatch_drain"][1] == "error"
    assert _contains(by_name["shutdown.step.dispatch_drain"], "dispatch_stuck")
    assert "shutdown.step.runtime" not in by_name
    assert "shutdown.step.ipc" not in by_name
