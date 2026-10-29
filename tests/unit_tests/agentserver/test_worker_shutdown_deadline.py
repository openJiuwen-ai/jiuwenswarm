# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Final Worker shutdown shares one deadline, including forced exit and IPC cleanup."""

import asyncio
import json
import subprocess
import sys
import time

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


@pytest.mark.parametrize("result", [{"ok": True, "absent": True}, {"kind": "shutdown.done", "ok": True}])
def test_shutdown_cli_emits_one_json_line_to_stdout(monkeypatch, capsys, caplog, result):
    from jiuwenswarm.server.supervisor import shutdown

    async def shutdown_worker(*, timeout):
        assert timeout == 2.0
        return result

    monkeypatch.setattr(sys, "argv", ["shutdown", "--shutdown-worker", "--timeout", "2"])
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", shutdown_worker)
    shutdown.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert output.out.count("\n") == 1
    assert output.err == ""
    assert not caplog.records


@pytest.mark.asyncio
async def test_final_shutdown_bounds_missing_ack_and_stuck_cleanup(monkeypatch, caplog):
    monkeypatch.setattr(supervisor_module, "_FINAL_SHUTDOWN_TIMEOUT", 0.06)
    monkeypatch.setattr(supervisor_module.logger, "handlers", [caplog.handler])
    budgets = []
    closed = asyncio.Event()

    class Connection:
        async def send(self, message):
            assert message["kind"] == "shutdown"

        async def close(self):
            closed.set()
            await asyncio.Event().wait()

    class Process:
        async def wait(self, *, timeout):
            budgets.append(timeout)
            return None

        async def stop(self, *, timeout):
            budgets.append(timeout)

    supervisor = RuntimeSupervisor(Readiness())
    supervisor._conn = Connection()
    supervisor._process = Process()
    supervisor._loop_task = asyncio.create_task(asyncio.Event().wait())
    started = time.monotonic()
    try:
        await asyncio.wait_for(supervisor.stop(terminate_worker=True), 0.5)
        assert time.monotonic() - started < 0.5
        assert budgets and max(budgets) < 0.01
        assert closed.is_set()
        assert supervisor._stopping and supervisor._conn is None
        assert "acknowledgement missing" in caplog.text
        assert "IPC/task cleanup" in caplog.text
    finally:
        supervisor._loop_task.cancel()
        await asyncio.gather(supervisor._loop_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_process_forced_exit_does_not_start_another_wait_budget(monkeypatch, caplog):
    from jiuwenswarm.server.supervisor import worker_process as worker_module

    monkeypatch.setattr(worker_module.logger, "handlers", [caplog.handler])
    calls = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    worker = WorkerProcess("unused", host="127.0.0.1", port=18092)
    worker._proc = Process()
    budgets = []

    async def wait(*, timeout=None):
        assert timeout is not None, "the kill/reap wait must also be bounded"
        budgets.append(timeout)
        await asyncio.sleep(timeout)
        return None

    monkeypatch.setattr(worker, "wait", wait)
    await asyncio.wait_for(worker.stop(timeout=0.04), 0.5)
    assert calls == ["terminate", "kill"]
    assert len(budgets) == 2 and budgets[1] < 0.01
    assert "exit could not be observed" in caplog.text


def test_final_exit_never_kills_a_reused_pid(monkeypatch):
    class ReusedProcess:
        def create_time(self):
            return 2.0

        def kill(self):
            pytest.fail("a reused PID must not be killed")

    monkeypatch.setattr(psutil, "Process", lambda pid: ReusedProcess())
    supervisor = RuntimeSupervisor(Readiness())
    supervisor._worker_pid = 123
    supervisor._worker_create_time = 1.0
    with pytest.raises(RuntimeError, match="PID identity changed"):
        supervisor._reap_validated_worker(deadline=time.monotonic() + 0.1)


@pytest.mark.asyncio
async def test_missing_shutdown_ack_forces_validated_native_child_exit(monkeypatch):
    monkeypatch.setattr(supervisor_module, "_FINAL_SHUTDOWN_TIMEOUT", 0.1)

    class Connection:
        async def send(self, message):
            pass

        async def close(self):
            pass

    # Use the actual interpreter, avoiding the Windows venv launcher's extra
    # process so the validated PID belongs to this exact test child.
    proc = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c",
        "import time; print('ready', flush=True); time.sleep(60)"], stdout=subprocess.PIPE)
    worker = WorkerProcess("unused", host="127.0.0.1", port=18092)
    worker._proc = proc
    supervisor = RuntimeSupervisor(Readiness())
    supervisor._conn = Connection()
    supervisor._process = worker
    supervisor._worker_pid = proc.pid
    supervisor._worker_create_time = psutil.Process(proc.pid).create_time()
    supervisor._loop_task = asyncio.create_task(asyncio.Event().wait())
    try:
        assert (await asyncio.to_thread(proc.stdout.readline)).strip() == b"ready"
        started = time.monotonic()
        await asyncio.wait_for(supervisor.stop(terminate_worker=True), 0.6)
        assert time.monotonic() - started < 0.6
        # Popen waits on this child's native process handle. A PID-only lookup
        # after exit can observe PID reuse or a retained Windows process object.
        assert await worker.wait(timeout=2) is not None
    finally:
        supervisor._loop_task.cancel()
        await asyncio.gather(supervisor._loop_task, return_exceptions=True)
        if proc.poll() is None:
            proc.kill()
            await asyncio.to_thread(proc.wait, 2)
        proc.stdout.close()
