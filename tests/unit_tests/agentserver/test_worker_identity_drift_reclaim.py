# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reclaiming a resident Worker whose create_time drifted across builds."""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest

from jiuwenswarm.server.supervisor import resident_lifecycle as rl
from jiuwenswarm.server.supervisor.identity import data_scope, deployment_scope


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def children(self, recursive=False):
        return []

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def socket_path(tmp_path):
    path = tmp_path / "runtime-worker.sock"
    path.write_text("stale")
    return str(path)


def _deadline():
    return time.monotonic() + 5.0


@pytest.mark.asyncio
async def test_force_retire_kills_verified_leftover_despite_create_time_drift(
    monkeypatch, socket_path,
):
    process = _FakeProcess()
    monkeypatch.setattr(rl.psutil, "Process", lambda pid=None: process)
    monkeypatch.setattr(rl, "_peer_matches", lambda proc, sock: None)

    reclaimed = await rl.force_retire_worker(
        socket_path, {"pid": os.getpid() + 1000}, deadline=_deadline(),
    )

    assert reclaimed is True
    assert process.terminated is True
    assert not Path(socket_path).exists()


@pytest.mark.asyncio
async def test_force_retire_refuses_when_pid_belongs_to_another_process(
    monkeypatch, socket_path,
):
    process = _FakeProcess()
    monkeypatch.setattr(rl.psutil, "Process", lambda pid=None: process)

    def refuse(proc, sock):
        raise RuntimeError("Worker lifecycle refused an unrelated process")

    monkeypatch.setattr(rl, "_peer_matches", refuse)

    reclaimed = await rl.force_retire_worker(
        socket_path, {"pid": os.getpid() + 1000}, deadline=_deadline(),
    )

    assert reclaimed is False
    assert process.terminated is False
    assert process.killed is False
    assert Path(socket_path).exists()


@pytest.mark.asyncio
async def test_force_retire_treats_missing_pid_as_reclaimed(monkeypatch, socket_path):
    def gone(pid=None):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(rl.psutil, "Process", gone)

    reclaimed = await rl.force_retire_worker(
        socket_path, {"pid": os.getpid() + 1000}, deadline=_deadline(),
    )

    assert reclaimed is True
    assert not Path(socket_path).exists()


def test_worker_process_still_rejects_create_time_drift(monkeypatch):
    class _DriftedProcess:
        def create_time(self):
            return 2.0

    monkeypatch.setattr(rl.psutil, "Process", lambda pid=None: _DriftedProcess())
    monkeypatch.setattr(rl, "_peer_matches", lambda proc, sock: None)

    hello = {
        "kind": "hello",
        "protocol": 2,
        "deployment_scope": deployment_scope(),
        "data_scope": data_scope(),
        "pid": os.getpid() + 1000,
        "process_created": 1.0,
    }
    with pytest.raises(RuntimeError, match="Worker PID identity changed"):
        rl.worker_process(hello, "/tmp/runtime-worker.sock", allow_legacy=True)
