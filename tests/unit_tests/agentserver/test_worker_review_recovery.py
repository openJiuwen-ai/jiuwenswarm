# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recovery must report refused cleanup without losing ownership or readiness."""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import resident_lifecycle, runtime_supervisor, worker_process
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@pytest.mark.asyncio
async def test_real_unverified_peer_survives_refused_cleanup_then_verified_recovery_replaces_it(
    monkeypatch, tmp_path,
):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.ipc.client import connect_with_retry

    data = tmp_path / "data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(utils, "_workspace_base_dir", data)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    path = str(tmp_path / "worker.pipe")
    owner = worker_process.WorkerProcess(path, host="127.0.0.1", port=18092, stub=True)
    readiness = Readiness()
    front = RuntimeSupervisor(readiness, socket_path=path, stub=True)
    try:
        await owner.spawn()
        peer = await connect_with_retry(path, timeout=15)
        hello = await asyncio.wait_for(peer.recv(), 5)
        pid = hello["pid"]
        created = psutil.Process(pid).create_time()
        peer.send = AsyncMock(wraps=peer.send)
        front._conn = peer
        readiness.mark_failed("HELLO validation interrupted")
        front._fatal = True
        assert await front.recover_from_failed(timeout=15) is False
        peer.send.assert_not_awaited()
        assert psutil.Process(pid).create_time() == created
        assert psutil.Process(pid).is_running()
        # Retry performs the complete HELLO validation before obtaining authority.
        assert await front.recover_from_failed(timeout=15) is True
        assert front.worker_pid == pid and not front.owns_process
        readiness.mark_failed("Runtime failed after validation")
        front._fatal = True
        assert await front.recover_from_failed(timeout=15) is True
        assert front.worker_pid != pid and front.owns_process
        assert await owner.wait(timeout=3) is not None
        assert not psutil.pid_exists(pid)
    finally:
        await front.stop(terminate_worker=True)
        await owner.stop()


@pytest.mark.asyncio
async def test_recovery_refuses_unvalidated_peer_without_resetting_failed_state():
    readiness = Readiness()
    readiness.mark_failed("HELLO has not been validated")
    front = RuntimeSupervisor(readiness)
    front._fatal = True
    front._fatal_reason = "HELLO has not been validated"
    front._restart_count = 3
    peer = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    front._conn = peer
    assert await front.recover_from_failed(timeout=.05) is False
    assert readiness.state is ReadinessState.FAILED
    assert front._fatal and front._fatal_reason == "HELLO has not been validated"
    assert front._restart_count == 3
    assert front._loop_task is None
    peer.send.assert_not_awaited()
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("exit not observed"), PermissionError("access denied")])
async def test_failed_cleanup_keeps_owned_handle_for_retry(monkeypatch, error):
    readiness = Readiness()
    readiness.mark_failed("runtime failed")
    front = RuntimeSupervisor(readiness)
    process = SimpleNamespace(pid=12345)
    front._process = process
    front._owns_process = True
    front._fatal = True
    close = AsyncMock()
    front._conn = SimpleNamespace(close=close)
    cleanup = AsyncMock(side_effect=error)
    monkeypatch.setattr(front, "_shutdown_owned_worker", cleanup)
    assert await front.recover_from_failed(timeout=.05) is False
    assert front._process is process and front._owns_process
    assert front._conn is None and front._validated_conn is None
    assert readiness.state is ReadinessState.FAILED and front._fatal
    assert front._loop_task is None
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_can_retry_after_retirement_failure_is_corrected(monkeypatch):
    readiness = Readiness()
    readiness.mark_failed("runtime failed")
    front = RuntimeSupervisor(readiness)
    front._fatal = True
    cleanup = AsyncMock(side_effect=[RuntimeError("exit not observed"), None])
    monkeypatch.setattr(front, "_shutdown_owned_worker", cleanup)

    async def bring_up():
        readiness.mark_agent_ready()

    monkeypatch.setattr(front, "_supervise", bring_up)
    assert await front.recover_from_failed(timeout=.1) is False
    assert readiness.state is ReadinessState.FAILED
    assert await front.recover_from_failed(timeout=.1) is True
    assert readiness.state is ReadinessState.AGENT_READY and not front._fatal
    assert cleanup.await_count == 2
    await front._loop_task


@pytest.mark.asyncio
async def test_slow_legacy_retirement_cannot_exceed_election_deadline(monkeypatch):
    front = RuntimeSupervisor(Readiness(), socket_path="owned-current-endpoint", connect_timeout=.04)
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello"}), close=AsyncMock())
    deadlines = []

    @asynccontextmanager
    async def lock(*_args):
        yield

    async def retire(*_args, deadline, **_kwargs):
        deadlines.append(deadline)
        await asyncio.sleep(max(0, deadline - time.monotonic()))

    monkeypatch.setattr(runtime_supervisor, "default_socket_path", lambda: front._socket_path)
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: ["old-one", "old-two"])
    monkeypatch.setattr(resident_lifecycle, "startup_lock", lock)
    monkeypatch.setattr(resident_lifecycle, "worker_process", lambda *_args, **_kwargs: (object(), 1))
    monkeypatch.setattr(resident_lifecycle, "retire_worker", retire)
    monkeypatch.setattr(runtime_supervisor, "try_connect", AsyncMock(return_value=peer))
    spawn = Mock()
    monkeypatch.setattr(runtime_supervisor, "WorkerProcess", spawn)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(front._connect_resident(), .3)
    assert time.monotonic() - started < .2
    assert deadlines and deadlines[0] <= started + .05
    peer.close.assert_awaited_once()
    spawn.assert_not_called()
