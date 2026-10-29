# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Historical discovery must not control an independently addressed Worker."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.ipc.client import connect_with_retry
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import resident_lifecycle, runtime_supervisor, shutdown, worker_process


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["startup", "shutdown"])
@pytest.mark.parametrize("failure", ["ownership", "connect", "hello", "lock"])
async def test_unknown_historical_endpoint_is_warned_and_preserved(monkeypatch, tmp_path, caplog, operation, failure):
    from contextlib import asynccontextmanager

    current, old = str(tmp_path / "current"), str(tmp_path / "legacy")
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "protocol": 1, "pid": 123}),
                           send=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(shutdown, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(runtime_supervisor, "default_socket_path", lambda: current)
    monkeypatch.setattr(shutdown, "default_socket_path", lambda: current)
    retire = AsyncMock(side_effect=AssertionError("Unknown processes must not receive shutdown"))
    monkeypatch.setattr(resident_lifecycle, "retire_worker", retire)
    monkeypatch.setattr(shutdown, "retire_worker", retire)
    validate = Mock(side_effect=RuntimeError("deployment ownership unconfirmed"))
    monkeypatch.setattr(resident_lifecycle, "worker_process", validate)
    monkeypatch.setattr(shutdown, "worker_process", validate)

    async def connect(path, **kwargs):
        if path == current:
            return None
        if failure == "connect":
            raise TimeoutError("historical connect timed out")
        return peer
    if failure == "hello":
        peer.recv.side_effect = TimeoutError("historical HELLO timed out")
    if failure == "lock":
        @asynccontextmanager
        async def locked(path, deadline):
            if path == old:
                raise TimeoutError("historical election timed out")
            yield
        monkeypatch.setattr(resident_lifecycle, "startup_lock", locked)
        monkeypatch.setattr(shutdown, "startup_lock", locked)
    monkeypatch.setattr(runtime_supervisor, "try_connect", connect)
    monkeypatch.setattr(shutdown, "try_connect", connect)
    monkeypatch.setattr(shutdown, "endpoint_is_listening", lambda _: False)
    if operation == "startup":
        readiness = Readiness()
        owner = runtime_supervisor.RuntimeSupervisor(readiness, socket_path=current)
        await owner._migrate_legacy_workers()
        assert not owner._fatal and not readiness.snapshot()["failed"]
    else:
        assert (await shutdown.shutdown_resident_worker(timeout=2))["ok"] is True
    retire.assert_not_awaited()
    peer.send.assert_not_awaited()
    assert old in caplog.text and "preserving" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["startup", "shutdown"])
async def test_proven_historical_owner_failure_still_blocks(monkeypatch, tmp_path, operation):
    current, old = str(tmp_path / "current"), str(tmp_path / "legacy")
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "stub": False, "pid": 123}),
                           close=AsyncMock())
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(shutdown, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(runtime_supervisor, "default_socket_path", lambda: current)
    monkeypatch.setattr(shutdown, "default_socket_path", lambda: current)
    validate = Mock(return_value=(object(), 1))
    monkeypatch.setattr(resident_lifecycle, "worker_process", validate)
    monkeypatch.setattr(shutdown, "worker_process", validate)
    retire = AsyncMock(side_effect=RuntimeError("owned cleanup failed"))
    monkeypatch.setattr(resident_lifecycle, "retire_worker", retire)
    monkeypatch.setattr(shutdown, "retire_worker", retire)
    async def connect(path, **kwargs):
        return None if path == current else peer
    monkeypatch.setattr(runtime_supervisor, "try_connect", connect)
    monkeypatch.setattr(shutdown, "try_connect", connect)
    monkeypatch.setattr(shutdown, "endpoint_is_listening", lambda _: False)
    with pytest.raises(RuntimeError, match="owned cleanup failed"):
        if operation == "startup":
            await runtime_supervisor.RuntimeSupervisor(Readiness(), socket_path=current)._migrate_legacy_workers()
        else:
            await shutdown.shutdown_resident_worker(timeout=2)
    retire.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2])
async def test_real_new_worker_runs_and_stops_while_unknown_old_worker_lives(tmp_path, monkeypatch, protocol):
    current, old = str(tmp_path / "current"), str(tmp_path / "legacy")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_SOCKET", current)
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(shutdown, "legacy_socket_paths", lambda: [old])
    legacy = worker_process.WorkerProcess(old, host="127.0.0.1", port=0, stub=True)
    front = runtime_supervisor.RuntimeSupervisor(Readiness(), stub=True, connect_timeout=15)
    monkeypatch.setattr(front, "_start_side_tasks", Mock())
    real_probe = runtime_supervisor.try_connect
    peers = []
    async def connect(path, **kwargs):
        conn = await real_probe(path, **kwargs)
        if path != old or conn is None:
            return conn
        real_recv = conn.recv
        async def recv():
            frame = await real_recv()
            if frame.get("kind") == "hello":
                frame.pop("deployment_scope", None)
                frame.pop("process_created", None)
                frame["protocol"] = protocol
            return frame
        peer = SimpleNamespace(recv=recv, send=AsyncMock(wraps=conn.send), close=conn.close)
        peers.append(peer)
        return peer
    monkeypatch.setattr(runtime_supervisor, "try_connect", connect)
    monkeypatch.setattr(shutdown, "try_connect", connect)
    try:
        await legacy.spawn()
        conn = await connect_with_retry(old, timeout=15)
        await conn.recv()
        await conn.close()
        await front._bring_up()
        assert legacy.returncode is None and front._worker_pid != legacy.pid
        await front._conn.send({"kind": "request", "payload": {
            "request_id": "independent", "req_method": "session.create"}})
        while True:
            frame = await asyncio.wait_for(front._conn.recv(), 5)
            if frame.get("kind") == "wire":
                assert frame["payload"]["echo"] == "session.create"
                break
        await front._conn.close()
        front._conn = None
        assert (await shutdown.shutdown_resident_worker(timeout=10))["ok"] is True
        assert await front._process.wait(timeout=2) == 0
        assert legacy.returncode is None
        for peer in peers:
            peer.send.assert_not_awaited()
    finally:
        if front._conn is not None:
            await front._conn.close()
        if front._process is not None:
            await front._process.stop(timeout=2)
        await legacy.stop(timeout=2)
