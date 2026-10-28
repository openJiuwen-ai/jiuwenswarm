# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Final Worker shutdown shares one deadline, including forced exit and IPC cleanup."""

import asyncio
import json
import subprocess
import sys
import time

import psutil
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", [False, True])
async def test_final_stop_reserves_time_to_reap_owned_child(monkeypatch, validated):
    clock = [0.0]
    monkeypatch.setattr(supervisor_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    waits = []
    stops = []

    class Process:
        pid = 123

        async def wait(self, *, timeout):
            waits.append(timeout)
            clock[0] += timeout
            return None

        async def stop(self, *, timeout):
            stops.append(timeout)
            assert timeout > 0, "graceful waiting consumed the forced-exit observation budget"

    peer = SimpleNamespace(send=AsyncMock())
    front = RuntimeSupervisor(Readiness())
    front._conn = peer
    front._validated_conn = peer if validated else None
    front._process = Process()
    await front._shutdown_owned_worker(deadline=1.0)
    assert stops and stops[0] >= .5
    assert sum(waits) + stops[0] <= 1.0
    if validated:
        peer.send.assert_awaited_once()
    else:
        peer.send.assert_not_awaited()
        assert waits == [], "an unverified owned child has received no graceful IPC request"


@pytest.mark.parametrize("exits", [False, True])
def test_validated_reaper_reserves_observation_time_and_reports_survivor(monkeypatch, exits):
    clock = [0.0]
    budgets = []
    killed = []
    monkeypatch.setattr(supervisor_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Process:
        def create_time(self):
            return 1.0

        def wait(self, *, timeout):
            budgets.append(timeout)
            clock[0] += timeout
            if len(budgets) == 1 or not exits:
                raise psutil.TimeoutExpired(timeout)
            return 0

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(psutil, "Process", lambda pid: Process())
    front = RuntimeSupervisor(Readiness())
    front._worker_pid, front._worker_create_time = 123, 1.0
    if exits:
        front._reap_validated_worker(deadline=1.0)
    else:
        with pytest.raises(RuntimeError, match="exit could not be observed"):
            front._reap_validated_worker(deadline=1.0)
    assert killed == [True]
    assert len(budgets) == 2 and budgets[1] > 0
    assert sum(budgets) <= 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lookup", "spawn_before", "spawn_after", "connect", "connect_timeout"])
async def test_owned_startup_is_cleaned_across_every_await(monkeypatch, tmp_path, phase):
    from jiuwenswarm.server.supervisor import identity, worker_process as worker_module

    entered = asyncio.Event()
    release = asyncio.Event()
    children = []
    monkeypatch.setattr(identity, "code_identity", lambda: "a" * 64)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))

    async def lookup(*args, **kwargs):
        if phase == "lookup":
            entered.set()
            await release.wait()
        return None

    class ObservedWorker(WorkerProcess):
        async def spawn(self):
            if phase == "spawn_before":
                entered.set()
                await release.wait()
            await super().spawn()
            children.append(self)
            if phase == "spawn_after":
                entered.set()
                await release.wait()

    async def slow_connect(*args, **kwargs):
        if phase == "connect_timeout":
            raise TimeoutError("IPC unavailable")
        entered.set()
        await release.wait()
        raise TimeoutError("IPC unavailable")

    monkeypatch.setattr(supervisor_module, "try_connect", lookup)
    monkeypatch.setattr(supervisor_module, "endpoint_is_listening", lambda _: False)
    monkeypatch.setattr(supervisor_module, "connect_with_retry", slow_connect)
    monkeypatch.setattr(supervisor_module, "WorkerProcess", ObservedWorker)
    monkeypatch.setattr(worker_module, "build_worker_command", lambda *a, **kw: [
        getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(60)"])
    front = RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), stub=True, max_restarts=0)
    start = None
    stop = None
    try:
        start = asyncio.create_task(front.run_until_ipc_ready())
        if phase == "connect_timeout":
            with pytest.raises(RuntimeError, match="IPC unavailable"):
                await asyncio.wait_for(start, 5)
        else:
            await asyncio.wait_for(entered.wait(), 5)
            stop = asyncio.create_task(front.stop(terminate_worker=True))
            while not front._stopping:
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stop, 3)
            await asyncio.wait_for(start, 3)
        assert len(children) == (0 if phase in {"lookup", "spawn_before"} else 1)
        assert all(child.returncode is not None for child in children)
        assert front._process is None and front._conn is None
    finally:
        release.set()
        for task in (start, stop, front._loop_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for child in children:
            await child.stop(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("reused", [False, True])
async def test_published_pid_retains_the_identity_checked_during_hello(monkeypatch, tmp_path, reused):
    from jiuwenswarm.server.supervisor import identity

    pid = 987654
    lookup_count = 0
    killed = Mock()

    def lookup(value=None):
        nonlocal lookup_count
        if value is None:
            return SimpleNamespace(username=lambda: "test-user")
        lookup_count += 1
        created = 2 if reused and lookup_count > 1 else 1
        return SimpleNamespace(pid=pid, create_time=lambda: created, username=lambda: "test-user",
            exe=lambda: sys.executable,
            cmdline=lambda: [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", str(tmp_path / "worker")],
            wait=lambda **kw: 0, kill=killed)

    hello = {"kind": "hello", "protocol": supervisor_module.PROTOCOL_VERSION, "pid": pid, "process_created": 1,
             "deployment_scope": identity.deployment_scope(), "data_scope": identity.data_scope(),
             "code_identity": "a" * 64, "stub": True}
    peer = SimpleNamespace(recv=AsyncMock(return_value=hello), send=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(identity, "code_identity", lambda: "a" * 64)
    monkeypatch.setattr(supervisor_module, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(psutil, "Process", lookup)
    front = RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), stub=True)
    monkeypatch.setattr(front, "_start_side_tasks", Mock())
    await front._bring_up()
    assert front._worker_create_time == 1 and lookup_count == 1
    if reused:
        with pytest.raises(RuntimeError, match="PID identity changed"):
            front._reap_validated_worker(deadline=time.monotonic() + .1)
    else:
        front._reap_validated_worker(deadline=time.monotonic() + .1)
    killed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("protocol", [1, 2])
async def test_public_complete_stop_during_unverified_hello_does_not_terminate_peer(
    monkeypatch, tmp_path, busy, protocol,
):
    from pathlib import Path
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.ipc.client import connect_with_retry

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / "data")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identities"))
    path = str(tmp_path / "worker")
    worker = WorkerProcess(path, host="127.0.0.1", port=0, stub=True)
    connection = None
    start = None
    pending = asyncio.Event()
    release = asyncio.Event()
    try:
        await worker.spawn()
        connection = await connect_with_retry(path, timeout=15)
        hello = await asyncio.wait_for(connection.recv(), 5)
        legacy = {key: value for key, value in hello.items() if key not in {
            "deployment_scope", "process_created",
        }}
        legacy["protocol"] = protocol

        async def delayed_hello():
            pending.set()
            await release.wait()
            return legacy

        async def close():
            release.set()
            await connection.close()

        peer = SimpleNamespace(recv=delayed_hello, send=AsyncMock(wraps=connection.send), close=close)
        constructor = Mock(side_effect=AssertionError("Must not replace an unknown peer"))
        monkeypatch.setattr(supervisor_module, "WorkerProcess", constructor)
        monkeypatch.setattr(supervisor_module, "try_connect", AsyncMock(return_value=None if busy else peer))
        monkeypatch.setattr(supervisor_module, "endpoint_is_listening", lambda _: True)
        monkeypatch.setattr(supervisor_module, "connect_with_retry", AsyncMock(return_value=peer))
        front = RuntimeSupervisor(Readiness(), socket_path=path, stub=True)
        start = asyncio.create_task(front.run_until_ipc_ready())
        await asyncio.wait_for(pending.wait(), 5)
        assert front._worker_pid is None and front._validated_conn is None
        with pytest.raises(RuntimeError, match="validation incomplete; termination refused"):
            await asyncio.wait_for(front.stop(terminate_worker=True), 2)
        await asyncio.wait_for(start, 2)
        peer.send.assert_not_awaited()
        constructor.assert_not_called()
        assert front._conn is None and front._validated_conn is None
        assert worker.returncode is None
    finally:
        release.set()
        if start is not None and not start.done():
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        if connection is not None:
            await connection.close()
        await worker.stop(timeout=2)


@pytest.mark.asyncio
async def test_complete_stop_cannot_reuse_an_old_connections_validation():
    front = RuntimeSupervisor(Readiness())
    peer = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    front._conn = peer
    front._validated_conn = SimpleNamespace()
    front._worker_pid = 123
    front._worker_create_time = 1
    reaper = Mock(side_effect=AssertionError("Must not reap before current peer validation"))
    front._reap_validated_worker = reaper
    with pytest.raises(RuntimeError, match="validation incomplete; termination refused"):
        await front.stop(terminate_worker=True)
    peer.send.assert_not_awaited()
    reaper.assert_not_called()


@pytest.mark.asyncio
async def test_complete_stop_can_reap_its_own_child_without_sending_unverified_ipc(monkeypatch, tmp_path):
    from pathlib import Path
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.ipc.client import connect_with_retry

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / "data")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identities"))
    monkeypatch.setattr(supervisor_module, "_FINAL_SHUTDOWN_TIMEOUT", .1)
    worker = WorkerProcess(str(tmp_path / "worker"), host="127.0.0.1", port=0, stub=True)
    peer = None
    native = None
    try:
        await worker.spawn()
        peer = await connect_with_retry(worker.socket_path, timeout=15)
        hello = await peer.recv()
        native = psutil.Process(hello["pid"])
        peer.send = AsyncMock(wraps=peer.send)
        front = RuntimeSupervisor(Readiness())
        front._conn = peer
        front._process = worker
        front._owns_process = True
        await asyncio.wait_for(front.stop(terminate_worker=True), .6)
        peer.send.assert_not_awaited()
        assert await worker.wait(timeout=2) is not None
        await asyncio.to_thread(native.wait, 2)
        assert not native.is_running() or native.status() == psutil.STATUS_ZOMBIE
    finally:
        if peer is not None:
            await peer.close()
        if native is not None and native.is_running():
            native.kill()
            await asyncio.to_thread(native.wait, 2)
        await worker.stop(timeout=2)


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
    clock = [0.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def wait(*, timeout=None):
        assert timeout is not None, "the kill/reap wait must also be bounded"
        budgets.append(timeout)
        clock[0] += timeout
        return None

    monkeypatch.setattr(worker, "wait", wait)
    await asyncio.wait_for(worker.stop(timeout=0.04), 0.5)
    assert calls == ["terminate", "kill"]
    assert len(budgets) == 2
    assert sum(budgets) <= .04, "grace and reap must not each start a fresh budget"
    assert 0 < budgets[0] < .04
    assert 0 < budgets[1] <= .04, "force-kill observation must share the original deadline"
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
    supervisor._validated_conn = supervisor._conn
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
