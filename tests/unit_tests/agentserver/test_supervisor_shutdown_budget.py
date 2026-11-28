# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Supervisor shutdown ordering and bounded waits."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


def _socket_path() -> str:
    return f"/tmp/jwsw-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"


class _FakeProcess:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.wait_calls = 0

    async def wait(self, *, timeout: float | None = None) -> int | None:
        self.wait_calls += 1
        return None

    async def stop(self, *, timeout: float = 5.0) -> None:
        self.stop_calls += 1


class _HangingConn:
    async def send(self, message: dict) -> None:
        return None

    async def close(self) -> None:
        await asyncio.Event().wait()


class _StuckProc:
    pid = 4242

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_stop_signal_prevents_restart_from_stopping_worker() -> None:
    supervisor = RuntimeSupervisor(
        Readiness(),
        socket_path=_socket_path(),
        max_restarts=3,
    )
    process = _FakeProcess()
    supervisor._process = process
    supervisor._owns_process = True

    restart = asyncio.create_task(
        supervisor._schedule_restart("ipc connection closed")
    )
    # Let _schedule_restart reach its yield point, then deliver the stop the
    # way a signal callback would: before the restart path touches the Worker.
    await asyncio.sleep(0)
    supervisor.request_stop()

    assert await asyncio.wait_for(restart, 1.0) is False
    assert process.stop_calls == 0
    assert supervisor.owns_process is True


@pytest.mark.asyncio
async def test_stop_is_bounded_when_supervise_loop_hangs(monkeypatch) -> None:
    monkeypatch.setattr(runtime_supervisor, "_STOP_BUDGET_SECONDS", 0.2)
    supervisor = RuntimeSupervisor(
        Readiness(),
        socket_path=_socket_path(),
    )
    hang = asyncio.Event()
    loop_task = asyncio.create_task(hang.wait())
    supervisor._loop_task = loop_task

    started = time.monotonic()
    await asyncio.wait_for(supervisor.stop(), 2.0)
    elapsed = time.monotonic() - started

    await asyncio.gather(loop_task, return_exceptions=True)
    assert elapsed < 1.0
    assert loop_task.cancelled()


@pytest.mark.asyncio
async def test_stop_is_bounded_when_ipc_close_hangs(monkeypatch) -> None:
    monkeypatch.setattr(runtime_supervisor, "_STOP_BUDGET_SECONDS", 0.2)
    supervisor = RuntimeSupervisor(
        Readiness(),
        socket_path=_socket_path(),
    )
    supervisor._conn = _HangingConn()

    started = time.monotonic()
    await asyncio.wait_for(supervisor.stop(), 2.0)

    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_worker_process_stop_bounds_post_kill_wait() -> None:
    process = WorkerProcess(_socket_path(), host="127.0.0.1", port=1)
    proc = _StuckProc()
    process._proc = proc

    started = time.monotonic()
    await asyncio.wait_for(process.stop(timeout=0.1), 2.0)

    assert proc.terminated is True
    assert proc.killed is True
    assert time.monotonic() - started < 1.0
