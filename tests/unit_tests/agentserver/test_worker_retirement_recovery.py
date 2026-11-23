# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""An unobserved Worker exit must retain its fence and retry retirement."""

import asyncio
import json
import threading
import time

import psutil
import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("ipc_mode,phase", [
    ("resident", "health"),
    ("front_loopback", "health"),
    ("front_loopback", "startup"),
])
async def test_unobserved_exit_retries_retirement_before_replacement(monkeypatch, ipc_mode, phase):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", ipc_mode)
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", .01)
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_MAX", .04)
    exit_observable = threading.Event()
    observations = []
    signals = []

    class Process:
        def create_time(self):
            return 1.0

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

        def wait(self, *, timeout):
            observations.append(exit_observable.is_set())
            if not exit_observable.is_set():
                raise psutil.TimeoutExpired(timeout, pid=123)
            return 0

    def process(pid):
        assert pid == 123
        return Process()

    monkeypatch.setattr(psutil, "Process", process)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=1)
    monkeypatch.setattr(supervisor, "_confirmed_worker_exit", lambda: False)
    bringups = []

    class Connection:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, _message):
            pass

        async def close(self):
            self.closed.set()

    async def bring_up():
        bringups.append(True)
        if len(bringups) == 1:
            supervisor._worker_pid, supervisor._worker_create_time = 123, 1.0
            supervisor._worker_instance = "retiring-worker"
            if phase == "startup":
                raise OSError("temporary bootstrap failure")
        else:
            assert exit_observable.is_set() and True in observations
        supervisor._conn = Connection()
        supervisor._validated_conn = supervisor._conn
        readiness.mark_agent_ready()

    async def pump():
        if len(bringups) == 1:
            supervisor._unhealthy_reason = "worker business loop stalled"
            return
        await supervisor._conn.closed.wait()

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_pump", pump)
    initial = asyncio.create_task(supervisor._supervise())
    supervisor._loop_task = initial
    try:
        await asyncio.wait_for(initial, 2)
        assert readiness.state is ReadinessState.FAILED
        if phase == "startup":
            assert "temporary bootstrap failure" in supervisor._fatal_reason
        assert supervisor._automatic_recovery_task is not None
        assert supervisor._retryable_failure
        assert supervisor._ipc_ready.is_set()
        async with asyncio.timeout(2):
            while len(observations) < 6:
                await asyncio.sleep(.01)
        assert len(bringups) == 1, "a live or unobserved Worker must fence replacement"
        assert supervisor.worker_pid == 123
        assert supervisor._worker_create_time == 1.0
        assert supervisor.worker_instance == "retiring-worker"
        assert readiness.state is ReadinessState.FAILED
        exit_observable.set()
        async with asyncio.timeout(2):
            while not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        assert len(bringups) == 2
        await asyncio.wait_for(supervisor._automatic_recovery_task, 2)
        assert not supervisor._fatal and not supervisor._retryable_failure
        assert signals.count("terminate") == 1
    finally:
        if supervisor._conn is not None:
            await supervisor._conn.close()
        supervisor._conn = None
        supervisor._validated_conn = None
        supervisor._worker_pid = None
        supervisor._worker_create_time = None
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_restarts", [0, 1])
async def test_unobserved_exit_respects_stop_and_disabled_restarts(monkeypatch, max_restarts):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "resident")
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", 60)
    supervisor = RuntimeSupervisor(Readiness(), max_restarts=max_restarts)

    async def bring_up():
        pass

    async def pump():
        supervisor._unhealthy_reason = "worker business loop stalled"

    async def retire():
        raise module.WorkerExitUnconfirmed("exit still pending")

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_pump", pump)
    monkeypatch.setattr(supervisor, "_retire_unhealthy_worker", retire)
    await asyncio.wait_for(supervisor._supervise(), 1)
    assert supervisor._readiness.state is ReadinessState.FAILED
    task = supervisor._automatic_recovery_task
    assert (task is not None) == bool(max_restarts)
    await supervisor.stop()
    assert task is None or task.done()
    assert supervisor._automatic_recovery_task is None and supervisor._process is None


@pytest.mark.asyncio
async def test_identity_failure_stays_nonretryable_when_startup_retirement_times_out(monkeypatch):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    supervisor = RuntimeSupervisor(Readiness())
    retire_calls = []

    async def bring_up():
        supervisor._worker_pid, supervisor._worker_create_time = 123, 1.0
        supervisor._worker_instance = "retiring-worker"
        raise module.WorkerIdentityError("invalid deployment identity")

    def reap(*, deadline, terminate=False):
        retire_calls.append((supervisor.worker_pid, supervisor._worker_create_time,
                             supervisor.worker_instance, terminate))
        raise module.WorkerExitUnconfirmed("exit still pending")

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_reap_validated_worker", reap)
    try:
        await asyncio.wait_for(supervisor._supervise(), 1)
        assert retire_calls == [(123, 1.0, "retiring-worker", True)]
        assert supervisor._readiness.state is ReadinessState.FAILED
        assert not supervisor._retryable_failure
        assert supervisor._automatic_recovery_task is None
        assert "invalid deployment identity" in supervisor._fatal_reason
        assert "exit still pending" in supervisor._fatal_reason
        assert supervisor._readiness.snapshot()["failed_reason"] == supervisor._fatal_reason
    finally:
        supervisor._clear_attached_worker_identity()
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ipc_mode,reattach", [
    ("resident", False), ("resident", True), ("front_loopback", False),
])
async def test_real_worker_retirement_timeout_recovers_without_reload(monkeypatch, tmp_path, ipc_mode, reattach):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", ipc_mode)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", .5)
    monkeypatch.setattr(module, "_FINAL_SHUTDOWN_TIMEOUT", 2)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder(), stub=True, max_restarts=1,
        socket_path=str(tmp_path / "runtime.pipe"))
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 20)
        assert await readiness.wait_agent_ready(5)
        if reattach:
            await supervisor.stop()
            readiness = Readiness()
            supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder(), stub=True, max_restarts=1,
                socket_path=str(tmp_path / "runtime.pipe"))
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 20)
            assert await readiness.wait_agent_ready(5)
            assert not supervisor.owns_process
        old_pid = supervisor.worker_pid
        old_worker = psutil.Process(old_pid)
        old_create_time = old_worker.create_time()
        reap = supervisor._reap_validated_worker

        def delayed_exit(*, deadline, terminate=False):
            # Inject only the OS observation failure. The real recovery path
            # must retire the still-running child before election/spawn.
            if terminate:
                raise module.WorkerExitUnconfirmed("injected exit observation timeout")
            return reap(deadline=deadline, terminate=terminate)

        monkeypatch.setattr(supervisor, "_reap_validated_worker", delayed_exit)
        supervisor._worker_last_beat = time.monotonic() - module._WORKER_STALL_RESTART - 1
        assert await supervisor._classify_worker_stall(time.monotonic())
        await asyncio.wait_for(supervisor._loop_task, 5)
        assert readiness.state is ReadinessState.FAILED
        assert supervisor._automatic_recovery_task is not None
        assert old_worker.is_running() and old_worker.create_time() == old_create_time
        assert supervisor.worker_pid == old_pid
        async with asyncio.timeout(30):
            while not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        await asyncio.wait_for(supervisor._automatic_recovery_task, 5)
        assert supervisor.worker_pid != old_pid
        assert not old_worker.is_running()
        assert supervisor._restart_count == 0
        frames = []

        class Client:
            async def send(self, value):
                frames.append(json.loads(value))

        request = AgentRequest(request_id="after-recovery", channel_id="web", user_id="owner",
            session_id="probe", req_method=ReqMethod.SESSION_CREATE, params={})
        client, send_lock = Client(), asyncio.Lock()
        supervisor.attach_gateway_connection(client, send_lock)
        await asyncio.wait_for(supervisor.dispatch_parsed_request(client, request, send_lock), 5)
        async with asyncio.timeout(5):
            while not frames:
                await asyncio.sleep(.01)
        assert any(frame.get("echo") == ReqMethod.SESSION_CREATE.value for frame in frames)
    finally:
        await supervisor.stop(terminate_worker=True)
