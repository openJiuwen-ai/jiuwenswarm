"""A ready-then-crash loop has a budget independent of bootstrap retries."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as module


@pytest.mark.asyncio
async def test_readiness_does_not_clear_the_post_ready_crash_window(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    supervisor = module.RuntimeSupervisor(Readiness())
    try:
        for index in range(3):
            supervisor._apply_readiness({"state": ReadinessState.AGENT_READY.value})
            assert supervisor._restart_count == 0
            supervisor._record_post_ready_exit("injected exit")
            now[0] += 1
            assert supervisor._fatal is (index == 2)
        assert supervisor._readiness.state is ReadinessState.FAILED
        assert supervisor._crash_retry_at == 700.0
        assert not await supervisor._schedule_restart("fourth request")
        assert supervisor._automatic_recovery_task is not None
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_old_exits_expire_and_intentional_shutdown_is_not_counted(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    supervisor = module.RuntimeSupervisor(Readiness())
    supervisor._record_post_ready_exit("never ready")
    assert not supervisor._post_ready_crashes
    for value in [100.0, 800.0, 1500.0, 2200.0]:
        now[0] = value
        supervisor._apply_readiness({"state": ReadinessState.AGENT_READY.value})
        supervisor._record_post_ready_exit("isolated exit")
        assert len(supervisor._post_ready_crashes) == 1
        assert not supervisor._fatal
    await supervisor.stop()


@pytest.mark.asyncio
async def test_terminal_error_preserves_session_request_and_unary_wire_compatibility():
    forwarder = SimpleNamespace(send=AsyncMock())
    supervisor = module.RuntimeSupervisor(Readiness(), forwarder=forwarder)
    request = AgentRequest(request_id="failed-request", channel_id="web", session_id="failed-session",
                           req_method=ReqMethod.CHAT_SEND, params={}, is_stream=True, user_id="owner")
    await supervisor._emit_error(object(), request, asyncio.Lock(), "worker crash circuit open", code="WORKER_UNSTABLE")
    wire = forwarder.send.await_args.args[0]
    assert wire["request_id"] == "failed-request"
    assert wire["session_id"] == "failed-session"
    assert wire["is_stream"] is False
    assert wire["is_final"] is True and wire["status"] == "failed"
    assert wire["body"]["details"]["code"] == "WORKER_UNSTABLE"
    await supervisor.stop()


@pytest.mark.asyncio
async def test_restore_error_code_survives_runtime_and_e2a_conversion():
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

    event = RuntimeEvent.error(request_id="restore-request", channel_id="web", session_id="restore-session",
        error=HistoryReplayError("context is too large", code="HISTORY_RESTORE_LIMIT"))
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    ws = SimpleNamespace(send=AsyncMock())
    assert await server._send_runtime_event(ws, event, asyncio.Lock(), streaming=True, sequence=0)
    wire = json.loads(ws.send.await_args.args[0])
    assert wire["is_final"] and wire["status"] == "failed"
    assert wire["is_stream"] is False
    assert wire["session_id"] == "restore-session"
    assert wire["body"]["details"]["code"] == "HISTORY_RESTORE_LIMIT"
    control = RuntimeEvent.control(request_id="control", channel_id="web", session_id="restore-session",
        payload={"event_type": "runtime.accepted"})
    assert control.payload == {"event_type": "runtime.accepted"}


@pytest.mark.asyncio
@pytest.mark.parametrize("ipc_mode", ["resident", "front_loopback"])
@pytest.mark.parametrize("exit_observation_delayed", [False, True])
async def test_real_ready_workers_open_the_crash_circuit(
    monkeypatch, tmp_path, ipc_mode, exit_observation_delayed,
):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", ipc_mode)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = module.RuntimeSupervisor(readiness, stub=True, stub_exit_on_request=True,
        socket_path=str(tmp_path / "worker.pipe"), forwarder=SimpleNamespace(send=AsyncMock()))
    if exit_observation_delayed:
        # IPC EOF can arrive before exit is visible to the process observer.
        monkeypatch.setattr(supervisor, "_confirmed_worker_exit", lambda: False)
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await readiness.wait_agent_ready(10)
        old_pids = []
        for index in range(3):
            old_pid = supervisor.worker_pid
            old_pids.append(old_pid)
            request = AgentRequest(request_id=f"crash-{index}", channel_id="web", session_id="crashing-session",
                req_method=ReqMethod.CHAT_SEND, params={}, is_stream=True, user_id="owner")
            await asyncio.wait_for(supervisor.dispatch_parsed_request(object(), request, asyncio.Lock()), 10)
            if index < 2:
                async with asyncio.timeout(15):
                    while supervisor.worker_pid == old_pid or not readiness.snapshot()["agent_ready"]:
                        await asyncio.sleep(.01)
        async with asyncio.timeout(15):
            while readiness.state is not ReadinessState.FAILED:
                await asyncio.sleep(.01)
        assert readiness.state is ReadinessState.FAILED
        assert len(supervisor._post_ready_crashes) == 3
        assert supervisor._crash_retry_at > module.time.monotonic()
        await asyncio.sleep(.3)
        assert supervisor.worker_pid in {old_pids[-1], None}
    finally:
        await supervisor.stop(terminate_worker=True)


@pytest.mark.asyncio
async def test_live_resident_reconnect_does_not_consume_the_crash_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "resident")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = module.RuntimeSupervisor(readiness, stub=True,
        socket_path=str(tmp_path / "worker.pipe"), forwarder=SimpleNamespace(send=AsyncMock()))
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await readiness.wait_agent_ready(10)
        original_pid = supervisor.worker_pid
        original_generation = supervisor._generation
        await supervisor._conn.close()
        async with asyncio.timeout(15):
            while supervisor._generation == original_generation or not readiness.snapshot()["agent_ready"]:
                await asyncio.sleep(.01)
        assert supervisor.worker_pid == original_pid
        assert not supervisor._post_ready_crashes
        assert supervisor._worker_reached_ready
    finally:
        await supervisor.stop(terminate_worker=True)


@pytest.mark.asyncio
async def test_manual_failed_recovery_does_not_consume_the_crash_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "resident")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = module.RuntimeSupervisor(readiness, stub=True,
        socket_path=str(tmp_path / "worker.pipe"), forwarder=SimpleNamespace(send=AsyncMock()))
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await readiness.wait_agent_ready(10)
        original_pid = supervisor.worker_pid
        readiness.mark_failed("explicit operator recovery")
        assert await supervisor.recover_from_failed(timeout=15)
        assert supervisor.worker_pid != original_pid
        assert not supervisor._post_ready_crashes
        assert supervisor._worker_reached_ready
    finally:
        await supervisor.stop(terminate_worker=True)
