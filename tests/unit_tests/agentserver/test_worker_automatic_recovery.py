# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Worker bootstrap failures recover after cooldown without operator reload."""
import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


@pytest.mark.asyncio
async def test_bootstrap_recovers_automatically_after_resource_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", .1)
    blocked = True
    attempts = []
    spawn = WorkerProcess.spawn

    async def failing_spawn(process):
        attempts.append(blocked)
        if blocked:
            raise OSError("temporary resource pressure")
        await spawn(process)

    monkeypatch.setattr(WorkerProcess, "spawn", failing_spawn)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, stub=True, socket_path=str(tmp_path / "runtime.pipe"),
        max_restarts=1, connect_timeout=10)
    frames = []

    class Connection:
        async def send(self, value):
            frames.append(value)

    try:
        with pytest.raises(RuntimeError, match="temporary resource pressure"):
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 5)
        admission = ExecutionAdmission(readiness)
        admission.attach_backend(supervisor)
        request = AgentRequest(request_id="failed-create", channel_id="web", session_id="probe",
            req_method=ReqMethod.SESSION_CREATE, params={}, user_id="owner")
        await admission.dispatch(Connection(), request, asyncio.Lock())
        assert "RUNTIME_FAILED" in frames[-1]
        assert readiness.state is ReadinessState.FAILED
        blocked = False
        async with asyncio.timeout(30):
            while not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        assert attempts[:2] == [True, True]
        assert False in attempts
        assert supervisor.worker_pid is not None
    finally:
        await supervisor.stop(terminate_worker=True)


@pytest.mark.asyncio
async def test_stop_cancels_delayed_recovery_without_starting_worker(monkeypatch):
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", 60)
    supervisor = RuntimeSupervisor(Readiness())
    supervisor._mark_restart_exhausted("temporary resource pressure")
    delayed = supervisor._automatic_recovery_task
    await supervisor.stop()
    assert delayed.done()
    assert supervisor._automatic_recovery_task is None
    assert supervisor._process is None


@pytest.mark.asyncio
async def test_identity_failure_does_not_start_automatic_recovery():
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness)
    supervisor._fatal = True
    supervisor._fatal_reason = "resident Worker belongs to a different deployment"
    readiness.mark_failed(supervisor._fatal_reason)
    assert not await supervisor._schedule_restart(supervisor._fatal_reason)
    assert supervisor._automatic_recovery_task is None
    await supervisor.stop()


@pytest.mark.asyncio
async def test_real_worker_exit_before_ipc_recovers_without_config_reload(monkeypatch, tmp_path):
    import os
    from pathlib import Path
    import psutil

    shim = tmp_path / "bootstrap"
    shim.mkdir()
    blocked = tmp_path / "blocked"
    blocked.touch()
    attempts = tmp_path / "attempts"
    (shim / "sitecustomize.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    with Path(os.environ['WORKER_ATTEMPTS']).open('a') as out: out.write(str(os.getpid())+'\\n')\n"
        "    if Path(os.environ['WORKER_BLOCKED']).exists(): os._exit(73)\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(source)]))
    monkeypatch.setenv("WORKER_ATTEMPTS", str(attempts))
    monkeypatch.setenv("WORKER_BLOCKED", str(blocked))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", .3)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, stub=True, socket_path=str(tmp_path / "runtime.pipe"),
        max_restarts=1, connect_timeout=1.5)
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 12)
        assert readiness.state is ReadinessState.FAILED
        failed = [int(pid) for pid in attempts.read_text().splitlines()]
        assert len(failed) == 2 and all(not psutil.pid_exists(pid) for pid in failed)
        blocked.unlink()
        async with asyncio.timeout(20):
            while not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        assert supervisor.worker_pid not in failed
        assert len(attempts.read_text().splitlines()) >= 3
    finally:
        await supervisor.stop(terminate_worker=True)


@pytest.mark.asyncio
async def test_confirmed_stalled_resident_is_replaced_instead_of_reattached(monkeypatch, tmp_path):
    import time
    import psutil

    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, stub=True, socket_path=str(tmp_path / "runtime.pipe"),
        max_restarts=1)
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await readiness.wait_agent_ready(5)
        old_pid = supervisor.worker_pid
        supervisor._worker_last_beat = time.monotonic() - module._WORKER_STALL_RESTART - 1
        assert await supervisor._classify_worker_stall(time.monotonic())
        async with asyncio.timeout(20):
            while supervisor.worker_pid == old_pid or not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        assert not psutil.pid_exists(old_pid)
    finally:
        await supervisor.stop(terminate_worker=True)
