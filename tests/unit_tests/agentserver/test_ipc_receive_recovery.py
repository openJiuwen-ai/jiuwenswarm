# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded receive bursts and recovery after a degraded resident reconnect."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from multiprocessing.connection import Pipe
from pathlib import Path

import psutil
import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc import stream as stream_module
from jiuwenswarm.server.ipc.protocol import IpcError, KIND_HEARTBEAT, KIND_WIRE, encode_frame
from jiuwenswarm.server.ipc.stream import PipeConnection, StreamConnection
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@asynccontextmanager
async def _pair(transport):
    server = None
    if transport == "pipe":
        parent, child = Pipe(duplex=True)
        sender, receiver = PipeConnection(parent), PipeConnection(child)
    else:
        accepted = asyncio.get_running_loop().create_future()

        def connected(reader, writer):
            accepted.set_result(StreamConnection(reader, writer))

        server = await asyncio.start_server(connected, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        sender = StreamConnection(reader, writer)
        receiver = await asyncio.wait_for(accepted, 2)
    try:
        yield sender, receiver
    finally:
        await asyncio.gather(sender.close(), receiver.close())
        if server is not None:
            server.close()
            await server.wait_closed()


def _message(index):
    return {"kind": KIND_WIRE, "sequence": f"{index:04d}"}


def _limit(monkeypatch, transport, limit):
    prefix = "_PIPE" if transport == "pipe" else "_STREAM"
    size = len(encode_frame(_message(0)))
    if limit == "messages":
        monkeypatch.setattr(stream_module, prefix + "_PENDING_MESSAGES", 3)
    elif limit == "bytes":
        monkeypatch.setattr(stream_module, prefix + "_PENDING_BYTES", 3 * size)
    return 256 if limit == "default" else 3


async def _until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["pipe", "stream"])
@pytest.mark.parametrize("limit", ["messages", "bytes", "default"])
async def test_receive_burst_drains_in_order_and_resumes_heartbeats(monkeypatch, transport, limit):
    capacity = _limit(monkeypatch, transport, limit)
    count = 600 if limit == "default" else 20
    async with _pair(transport) as (sender, receiver):
        receiver.set_heartbeat_responder(lambda _m: receiver.send_direct({"kind": KIND_HEARTBEAT}))
        await sender.send(_message(0))
        assert (await receiver.recv())["sequence"] == "0000"

        async def publish():
            for index in range(1, count + 1):
                await sender.send(_message(index))
                if index == capacity + 1:
                    await sender.send({"kind": KIND_HEARTBEAT})

        publishing = asyncio.create_task(publish())
        try:
            await _until(lambda: receiver.diagnostics()["receive_pending"] == capacity)
            # A scheduler pause must not turn an otherwise drainable burst into EOF.
            await asyncio.sleep(0.03)
            assert receiver._reader_error is None
            received = []
            async with asyncio.timeout(10):
                for _ in range(count):
                    received.append((await receiver.recv())["sequence"])
                    snapshot = receiver.diagnostics()
                    assert snapshot["receive_pending"] <= capacity
                    if limit == "bytes":
                        assert snapshot["receive_pending_bytes"] <= 3 * len(encode_frame(_message(0)))
                    await asyncio.sleep(0)
                await publishing
                assert (await sender.recv())["kind"] == KIND_HEARTBEAT
            assert received == [f"{index:04d}" for index in range(1, count + 1)]
            assert receiver._reader_error is None
        finally:
            publishing.cancel()
            await asyncio.gather(publishing, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["pipe", "stream"])
@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_sustained_receive_overload_remains_bounded_and_reports_pressure(
    monkeypatch, caplog, transport, limit,
):
    _limit(monkeypatch, transport, limit)
    monkeypatch.setattr(stream_module, "_RECEIVE_BACKLOG_TIMEOUT", 0.05, raising=False)
    async with _pair(transport) as (sender, receiver):
        await sender.send(_message(0))
        await receiver.recv()
        for index in range(1, 5):
            await sender.send(_message(index))
        await _until(lambda: receiver._reader_error is not None)
        snapshot = receiver.diagnostics()
        assert snapshot["receive_pending"] == 3
        assert snapshot["receive_pending_bytes"] == 3 * len(encode_frame(_message(0)))
        assert [(await receiver.recv())["sequence"] for _ in range(3)] == ["0001", "0002", "0003"]
        with pytest.raises(IpcError, match="ipc business receive backlog exceeded"):
            await receiver.recv()
        assert "pending_messages=3" in caplog.text
        assert "incoming_kind=wire" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["pipe", "stream"])
async def test_close_wakes_receiver_waiting_for_backlog_space(monkeypatch, transport):
    _limit(monkeypatch, transport, "messages")
    monkeypatch.setattr(stream_module, "_RECEIVE_BACKLOG_TIMEOUT", 10, raising=False)
    async with _pair(transport) as (sender, receiver):
        await sender.send(_message(0))
        await receiver.recv()
        for index in range(1, 5):
            await sender.send(_message(index))
        await _until(lambda: receiver.diagnostics()["receive_pending"] == 3)
        await asyncio.sleep(0.02)
        await asyncio.wait_for(receiver.close(), 1)
        assert receiver.diagnostics()["receive_pending"] == 0
        if transport == "pipe":
            await _until(lambda: all(not thread.is_alive() for thread in receiver._reader_pool._threads))
        else:
            assert receiver._reader_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["AGENT_READY", "DEGRADED"])
async def test_ready_resident_reconnect_resets_failure_streak(state):
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=3)
    try:
        for _ in range(4):
            assert await supervisor._schedule_restart("transient IPC disconnect")
            supervisor._apply_readiness({"state": state, "agent_ready": True})
            assert readiness.snapshot()["agent_ready"]
            assert supervisor._restart_count == 0
            assert not supervisor._fatal
        assert readiness.state.value == state
    finally:
        await supervisor.stop()


def test_degraded_without_execution_readiness_does_not_clear_failure():
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness)
    readiness.mark_failed("runtime load failed")
    supervisor._fatal = True
    supervisor._restart_count = 3
    supervisor._apply_readiness({"state": "DEGRADED", "agent_ready": False,
        "runtime_phase": "IPC_READY", "capability_readiness": {"image": "failed"}})
    assert readiness.state is ReadinessState.FAILED
    assert supervisor._fatal and supervisor._restart_count == 3
    assert readiness.snapshot()["capability_readiness"] == {"image": "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["pipe", "stream"])
@pytest.mark.parametrize("crash_circuit_open", [False, True])
async def test_exhausted_receive_failure_closes_transport_and_cancels_watchers(
    monkeypatch, transport, crash_circuit_open,
):
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=0)
    if crash_circuit_open:
        supervisor._crash_retry_at = time.monotonic() + 60
    waiting = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    pending = asyncio.get_running_loop().create_future()
    supervisor._pending["request"] = pending
    async with _pair(transport) as (sender, receiver):
        async def bring_up():
            supervisor._conn = receiver
            supervisor._side_tasks = [waiting]

        async def pump():
            raise IpcError("ipc business receive backlog exceeded")

        monkeypatch.setattr(supervisor, "_bring_up", bring_up)
        monkeypatch.setattr(supervisor, "_pump", pump)
        monkeypatch.setattr(supervisor, "_log_diagnostics", lambda _reason: None)
        try:
            await asyncio.wait_for(supervisor._supervise(), 2)
            assert readiness.state is ReadinessState.FAILED
            assert pending.done() and not pending.result()["ok"]
            assert pending.result()["code"] == ("WORKER_UNSTABLE" if crash_circuit_open else "WORKER_CLOSED")
            assert receiver._closed
            assert waiting.cancelled() and supervisor._side_tasks == []
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(sender.recv(), 2)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await supervisor.stop()


@pytest.mark.asyncio
async def test_degraded_readiness_keeps_the_confirmed_process_crash_budget():
    supervisor = RuntimeSupervisor(Readiness())
    try:
        for index in range(3):
            supervisor._apply_readiness({"state": "DEGRADED", "agent_ready": True})
            assert supervisor._restart_count == 0
            supervisor._record_post_ready_exit("confirmed process exit")
            assert supervisor._fatal is (index == 2)
        assert len(supervisor._post_ready_crashes) == 3
        assert supervisor._crash_retry_at > time.monotonic()
        assert supervisor._readiness.state is ReadinessState.FAILED
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_real_degraded_resident_survives_four_ipc_reconnects(monkeypatch, tmp_path):
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    # Change only the test Worker's readiness frame; IPC, Supervisor and the
    # subprocess entrypoint remain the production implementations.
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from jiuwenswarm.server.ipc.stream import PipeConnection, StreamConnection\n"
        "    def wrap_send(original):\n"
        "        async def send(self, message):\n"
        "            if message.get('kind') == 'readiness' and message.get('state') == 'AGENT_READY':\n"
        "                message = dict(message, state='DEGRADED', agent_ready=True)\n"
        "            await original(self, message)\n"
        "        return send\n"
        "    for connection in (PipeConnection, StreamConnection):\n"
        "        connection.send = wrap_send(connection.send)\n",
        encoding="utf-8",
    )
    source = Path(__file__).resolve().parents[3]
    python_paths = [str(bootstrap), str(source)]
    if os.environ.get("PYTHONPATH"):
        python_paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(python_paths))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "resident")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder(), stub=True,
        socket_path=str(tmp_path / "runtime.pipe"), max_restarts=3)

    class Gateway:
        def __init__(self):
            self.frames = []

        async def send(self, value):
            self.frames.append(json.loads(value) if isinstance(value, str) else value)

    gateway = Gateway()
    lock = asyncio.Lock()
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 20)
        assert await readiness.wait_agent_ready(10)
        pid = supervisor.worker_pid
        created = psutil.Process(pid).create_time()
        for index in range(4):
            generation = supervisor._generation
            await supervisor._conn.close()
            await _until(lambda: supervisor._generation > generation
                and readiness.state is ReadinessState.DEGRADED, timeout=15)
            assert supervisor.worker_pid == pid
            assert psutil.Process(pid).create_time() == created
            assert supervisor._restart_count == 0
            request = AgentRequest(request_id=f"reconnect-{index}", channel_id="web",
                session_id="degraded-resident", user_id="owner", req_method=ReqMethod.CHAT_SEND,
                params={}, is_stream=True)
            await asyncio.wait_for(supervisor.dispatch_parsed_request(gateway, request, lock), 5)
            assert any(frame.get("request_id") == request.request_id
                and frame.get("echo") == "chat.send" for frame in gateway.frames)
        assert not supervisor._fatal
    finally:
        await supervisor.stop(terminate_worker=True)
    assert not psutil.pid_exists(pid)
