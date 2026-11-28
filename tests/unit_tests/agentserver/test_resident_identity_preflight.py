# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Source probing must not consume a resident peer's handshake deadline."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
from jiuwenswarm.server.ipc.startup import await_startup_acceptance
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import identity, identity_probe, resident_lifecycle
from jiuwenswarm.server.supervisor import runtime_supervisor as module


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_slow_identity_probe_finishes_before_resident_acceptance_window(monkeypatch, tmp_path, busy):
    hello = {"kind": "hello", "protocol": PROTOCOL_VERSION, "pid": 123,
             "instance": "resident", "generation": 3, "data_scope": "scope",
             "deployment_scope": "deployment", "code_identity": "code", "stub": True}
    to_front, to_worker = asyncio.Queue(), asyncio.Queue()
    worker_peer = SimpleNamespace(recv=to_worker.get)
    worker = None
    order = []

    async def handshake():
        await to_front.put(hello)
        await await_startup_acceptance(worker_peer, hello, timeout=.04)

    async def deliver(frame):
        await to_worker.put(frame)
        await worker

    peer = SimpleNamespace(recv=to_front.get, send=deliver, close=AsyncMock())

    async def connect(*_args, **_kwargs):
        nonlocal worker
        order.append("connect")
        worker = asyncio.create_task(handshake())
        return peer

    async def probe():
        order.append("probe-start")
        await asyncio.sleep(.06)
        order.append("probe-done")
        return "code", None

    monkeypatch.setattr(identity_probe, "probe_identity", probe)
    monkeypatch.setattr(module, "endpoint_is_listening", lambda _path: True)
    monkeypatch.setattr(module, "try_connect", AsyncMock(return_value=None) if busy else connect)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(identity, "data_scope", lambda: "scope")
    monkeypatch.setattr(identity, "deployment_scope", lambda: "deployment")
    monkeypatch.setattr(resident_lifecycle, "worker_process", Mock(return_value=(SimpleNamespace(pid=123), 1.0)))
    spawn = Mock()
    monkeypatch.setattr(module, "WorkerProcess", spawn)
    front = module.RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), stub=True)
    try:
        received, process, created = await front._connect_resident()
        assert received == hello and process.pid == 123 and created == 1.0
        assert order == ["probe-start", "probe-done", "connect"]
        assert front._conn is peer and not front.owns_process
        spawn.assert_not_called()
    finally:
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "stop", "deadline", "cancel"])
async def test_identity_preflight_cannot_open_or_stop_a_resident_on_failure(monkeypatch, tmp_path, outcome):
    front = module.RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), connect_timeout=.03)
    entered = asyncio.Event()

    async def probe():
        entered.set()
        if outcome == "failure":
            raise OSError("identity probe failed")
        if outcome == "stop":
            front._stopping = True
        await asyncio.sleep(.05 if outcome == "deadline" else 10 if outcome == "cancel" else 0)
        return "code", None

    connect = AsyncMock()
    spawn = Mock()
    monkeypatch.setattr(identity_probe, "probe_identity", probe)
    monkeypatch.setattr(module, "endpoint_is_listening", lambda _path: True)
    monkeypatch.setattr(module, "try_connect", connect)
    monkeypatch.setattr(module, "WorkerProcess", spawn)
    task = asyncio.create_task(front._connect_resident())
    await asyncio.wait_for(entered.wait(), 1)
    if outcome == "cancel":
        task.cancel()
    error = {"failure": OSError, "stop": RuntimeError, "deadline": TimeoutError, "cancel": asyncio.CancelledError}[outcome]
    with pytest.raises(error):
        await task
    connect.assert_not_awaited()
    spawn.assert_not_called()
    assert front._conn is None and front._process is None and not front.owns_process


@pytest.mark.asyncio
async def test_real_resident_survives_front_restart_with_ten_second_identity_probe(monkeypatch, ipc_dir, tmp_path):
    path = str(ipc_dir / "slow-preflight")
    first = module.RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
    second = module.RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
    owned = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 20)
        assert await first._readiness.wait_agent_ready(3)
        owned = first._process
        original = (first.worker_pid, first.worker_instance, first._worker_create_time)
        await first.stop()
        original_probe = identity_probe.probe_identity

        async def slow_probe():
            result = await original_probe()
            await asyncio.sleep(10.2)
            return result

        monkeypatch.setattr(identity_probe, "probe_identity", slow_probe)
        began = time.monotonic()
        await asyncio.wait_for(second.run_until_ipc_ready(), 22)
        elapsed = time.monotonic() - began
        assert await second._readiness.wait_agent_ready(3)
        assert (second.worker_pid, second.worker_instance, second._worker_create_time) == original
        assert not second.owns_process and second._restart_count == 0
        assert elapsed >= 10.2
        (tmp_path / "resident_preflight.json").write_text(json.dumps({
            "probe_delay_seconds": 10.2, "reattach_elapsed_seconds": elapsed,
            "same_worker_pid": second.worker_pid,
        }), encoding="utf-8")
    finally:
        try:
            await second.stop(terminate_worker=True)
        finally:
            await first.stop()
            if owned is not None:
                await owned.stop()
