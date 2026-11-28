# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Front startup/shutdown wiring."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import types
import zlib
from pathlib import Path

import pytest

from jiuwenswarm.runtime_trace import lifecycle, state
from jiuwenswarm.server import app_agentserver


class _FakeRecorder:
    def __init__(self) -> None:
        self.spans = []
        self.startup_handle = {"name": "startup"}
        self.shutdown_handle = None

    def start(self, name, *, level="normal", parent=None, mono_start=None, attributes=None):
        return {
            "name": name,
            "level": level,
            "parent": parent,
            "mono_start": mono_start,
            "attributes": dict(attributes or {}),
        }

    def end(self, handle, outcome, *, mono_end=None, attributes=None):
        handle["outcome"] = outcome
        handle["mono_end"] = mono_end
        handle["end_attributes"] = dict(attributes or {})
        self.spans.append(handle)


def _write_enabled_config(workspace: Path) -> None:
    config_dir = workspace / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "runtime_observability:\n  enabled: true\n  slow_task_ms: 1000\n",
        encoding="utf-8",
    )


@pytest.fixture()
def clean_state():
    import signal

    saved_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, getattr(signal, "SIGBREAK", None))
        if signum is not None
    }
    state.clear_state()
    with lifecycle._lock:
        lifecycle._components = None
    try:
        yield
    finally:
        for signum, handler in saved_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if lifecycle._components is not None:
            try:
                lifecycle.shutdown_runtime_trace(
                    deadline_mono=time.monotonic() + 0.2
                )
            except Exception:  # noqa: BLE001
                pass
            lifecycle._reset()
        state.clear_state()


def test_replay_front_startup_phases_creates_config_and_import_spans() -> None:
    recorder = _FakeRecorder()
    app_agentserver._replay_front_startup_phases(recorder, 9_000_000_000)
    names = [span["name"] for span in recorder.spans]
    assert "startup.phase.config" in names
    assert "startup.phase.dotenv_parsed" in names
    assert "startup.phase.entry_module_ready" in names
    assert all(span["parent"] is recorder.startup_handle for span in recorder.spans)


def test_end_startup_phase_marks_error_reason() -> None:
    recorder = _FakeRecorder()
    app_agentserver._end_startup_phase(
        recorder, "startup.phase.ws_listen", 1, "error", "ws_bind_failed"
    )
    span = recorder.spans[0]
    assert span["outcome"] == "error"
    assert span["end_attributes"]["runtime.reason_code"] == "ws_bind_failed"


def test_close_startup_root_is_noop_without_recorder(monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "current_recorder", lambda: None)
    app_agentserver._close_startup_root(None, "error", "ws_bind_failed")
    app_agentserver._close_startup_root(
        types.SimpleNamespace(), "error", "ws_bind_failed"
    )


@pytest.mark.asyncio
async def test_start_supervisor_passes_recorder(monkeypatch) -> None:
    captured = {}

    class _Supervisor:
        def __init__(self, readiness, **kwargs):
            captured.update(kwargs)

        async def run_until_ipc_ready(self):
            return None

    monkeypatch.setattr(
        "jiuwenswarm.server.supervisor.runtime_supervisor.RuntimeSupervisor",
        _Supervisor,
    )
    front = types.SimpleNamespace(
        readiness=types.SimpleNamespace(),
        forwarder=None,
        attach_runtime_backend=None,
    )
    recorder = object()
    await app_agentserver._start_supervisor(
        front, "127.0.0.1", 18092, recorder=recorder
    )
    assert captured["recorder"] is recorder


@pytest.mark.asyncio
async def test_run_graceful_shutdown_records_steps(
    tmp_path: Path, monkeypatch, clean_state
) -> None:
    _write_enabled_config(tmp_path)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")

    class _ImmediateEvent:
        def set(self) -> None:
            return None

        async def wait(self) -> bool:
            # Let the supervisor watcher finish before shutdown starts.
            for _ in range(10):
                await asyncio.sleep(0)
            return True

    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        def Event():
            return _ImmediateEvent()

    monkeypatch.setattr(app_agentserver, "asyncio", _AsyncioProxy())

    class _Front:
        def __init__(self, host: str, port: int) -> None:
            self.readiness = types.SimpleNamespace(
                snapshot=lambda: {"control_ready": True}
            )

        async def start(self) -> None:
            return None

        def begin_drain(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _Supervisor:
        async def stop(self) -> None:
            return None

    async def _fake_start_supervisor(front, host, port, recorder=None):
        return _Supervisor()

    monkeypatch.setattr(
        app_agentserver, "_start_supervisor", _fake_start_supervisor
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront", _Front
    )
    monkeypatch.setattr(app_agentserver, "_configure_openjiuwen_logging", lambda: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.cli_env_patch.install_shell_patch",
        lambda: None,
    )

    await app_agentserver._run("127.0.0.1", 1)

    database = next((tmp_path / "runtime").glob("front/*.sqlite3"))
    conn = sqlite3.connect(database)
    try:
        rows = dict(
            conn.execute("SELECT name, outcome FROM span_records")
        )
    finally:
        conn.close()
    assert rows["startup"] == "success"
    assert rows["shutdown"] == "success"
    for step in (
        "shutdown.step.drain",
        "shutdown.step.supervisor_stop",
        "shutdown.step.front_stop",
    ):
        assert rows[step] == "success"


@pytest.mark.asyncio
async def test_run_cancelled_after_startup_still_drains_shutdown(
    tmp_path: Path, monkeypatch, clean_state
) -> None:
    _write_enabled_config(tmp_path)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")

    events = []

    class _BlockingEvent:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        def set(self) -> None:
            return None

        async def wait(self) -> bool:
            self.entered.set()
            await asyncio.Event().wait()
            return True

    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        def Event():
            event = _BlockingEvent()
            events.append(event)
            return event

    monkeypatch.setattr(app_agentserver, "asyncio", _AsyncioProxy())

    class _Front:
        def __init__(self, host: str, port: int) -> None:
            self.readiness = types.SimpleNamespace(
                snapshot=lambda: {"control_ready": True}
            )

        async def start(self) -> None:
            return None

        def begin_drain(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _Supervisor:
        async def stop(self) -> None:
            return None

    async def _fake_start_supervisor(front, host, port, recorder=None):
        return _Supervisor()

    monkeypatch.setattr(
        app_agentserver, "_start_supervisor", _fake_start_supervisor
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront", _Front
    )
    monkeypatch.setattr(app_agentserver, "_configure_openjiuwen_logging", lambda: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.cli_env_patch.install_shell_patch",
        lambda: None,
    )

    task = asyncio.create_task(app_agentserver._run("127.0.0.1", 1))
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0)
    assert events, "stop event was not created"
    await asyncio.wait_for(events[0].entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    database = next((tmp_path / "runtime").glob("front/*.sqlite3"))
    conn = sqlite3.connect(database)
    try:
        rows = dict(
            conn.execute("SELECT name, outcome FROM span_records")
        )
        closed = conn.execute(
            "SELECT closed_at IS NOT NULL FROM meta"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows["startup"] == "success"
    assert rows["shutdown"] in {"cancelled", "success"}
    assert rows["shutdown.step.drain"] == "success"
    assert rows["shutdown.step.supervisor_stop"] == "success"
    assert rows["shutdown.step.front_stop"] == "success"
    assert closed == 1


@pytest.mark.asyncio
async def test_run_bind_failure_closes_and_drains_trace(
    tmp_path: Path, monkeypatch, clean_state
) -> None:
    _write_enabled_config(tmp_path)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")

    class _FakeFront:
        def __init__(self, host: str, port: int) -> None:
            self.readiness = types.SimpleNamespace()

        async def start(self) -> None:
            raise OSError("bind failed")

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "jiuwenswarm.server.front.server.AgentServerFront", _FakeFront
    )
    monkeypatch.setattr(app_agentserver, "_configure_openjiuwen_logging", lambda: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.cli_env_patch.install_shell_patch",
        lambda: None,
    )

    with pytest.raises(OSError):
        await app_agentserver._run("127.0.0.1", 1)

    database = next((tmp_path / "runtime").glob("front/*.sqlite3"))
    conn = sqlite3.connect(database)
    try:
        rows = list(
            conn.execute("SELECT name, outcome, raw_json FROM span_records")
        )
    finally:
        conn.close()
    by_name = {row[0]: row for row in rows}
    assert by_name["startup"][1] == "error"
    assert b"ws_bind_failed" in zlib.decompress(by_name["startup"][2])
    assert by_name["startup.phase.front_init"][1] == "success"
    assert by_name["startup.phase.trace_attach"][1] == "success"
    assert by_name["startup.phase.ws_listen"][1] == "error"
    assert b"ws_bind_failed" in zlib.decompress(
        by_name["startup.phase.ws_listen"][2]
    )
