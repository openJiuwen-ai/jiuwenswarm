# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-level runtime trace coverage."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from jiuwenswarm.runtime_trace import lifecycle, state
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings
from jiuwenswarm.server.ipc.client import endpoint_is_listening
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


def _settings() -> RuntimeTraceSettings:
    return RuntimeTraceSettings(
        enabled=True,
        slow_task_ms=1000,
        retention_days=7,
        exporter="none",
    )


async def _wait_for_socket(path: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if endpoint_is_listening(path):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("stub worker did not bind its socket")


def _rows(database: Path) -> list[tuple]:
    conn = sqlite3.connect(database)
    try:
        return list(conn.execute("SELECT name, outcome FROM span_records"))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_front_events_with_stub(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")
    with lifecycle._lock:
        lifecycle._components = None
    state.clear_state()

    socket_path = str(tmp_path / "worker.sock")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        socket_path,
        "--stub",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    supervisor = None
    try:
        await _wait_for_socket(socket_path)
        recorder = lifecycle.attach_runtime_trace(
            role="front", settings=_settings()
        )
        assert recorder is not None
        supervisor = RuntimeSupervisor(
            Readiness(),
            socket_path=socket_path,
            stub=True,
            max_restarts=0,
            recorder=recorder,
        )
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 10)
        assert supervisor.owns_process is False
        await supervisor.stop()
        supervisor = None
    finally:
        if supervisor is not None:
            await supervisor.stop()
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        lifecycle.close_open_startup_root(outcome="success")
        lifecycle.start_shutdown_root()
        lifecycle.shutdown_runtime_trace(outcome="success")
        state.clear_state()
        with lifecycle._lock:
            lifecycle._components = None

    database = next((tmp_path / "runtime").glob("front/*.sqlite3"))
    names = [row[0] for row in _rows(database)]
    assert "worker.attach" in names
    assert "worker.spawn" not in names
    # A stub Worker never creates a run database.
    assert list((tmp_path / "runtime").glob("worker/*.sqlite3")) == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JIUWENSWARM_RUNTIME_E2E") != "1",
    reason="real Worker startup needs model credentials; set JIUWENSWARM_RUNTIME_E2E=1",
)
async def test_two_databases_with_real_worker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")
    with lifecycle._lock:
        lifecycle._components = None
    state.clear_state()

    socket_path = str(tmp_path / "worker.sock")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        socket_path,
        "--host",
        "127.0.0.1",
        "--port",
        "18093",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    supervisor = None
    try:
        await _wait_for_socket(socket_path, timeout=30)
        recorder = lifecycle.attach_runtime_trace(
            role="front", settings=_settings()
        )
        assert recorder is not None
        supervisor = RuntimeSupervisor(
            Readiness(),
            socket_path=socket_path,
            host="127.0.0.1",
            port=18093,
            max_restarts=0,
            recorder=recorder,
        )
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 120)
        await supervisor.stop()
        supervisor = None
    finally:
        if supervisor is not None:
            await supervisor.stop()
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        lifecycle.close_open_startup_root(outcome="success")
        lifecycle.start_shutdown_root()
        lifecycle.shutdown_runtime_trace(outcome="success")
        state.clear_state()
        with lifecycle._lock:
            lifecycle._components = None

    databases = list((tmp_path / "runtime").glob("*/*.sqlite3"))
    roles = {database.parent.name for database in databases}
    assert roles == {"front", "worker"}
