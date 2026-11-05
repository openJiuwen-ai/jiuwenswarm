# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker loop liveness: off-loop heartbeats plus Supervisor classification.

The heartbeat fast path must keep transport liveness visible while the
Worker's business event loop is blocked, and the Supervisor must tell a
stalled loop from a dead pipe before it replaces the Worker.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from multiprocessing.connection import Pipe
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import IpcError, KIND_HEARTBEAT, KIND_WIRE, encode_frame
from jiuwenswarm.server.ipc import stream as stream_module
from jiuwenswarm.server.ipc.stream import PipeConnection
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker.diagnostics import LoopBeat, heartbeat_reply
from jiuwenswarm.server.worker import diagnostics
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


@pytest.fixture(autouse=True)
def _capture_supervisor_logs(caplog):
    module.logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        module.logger.removeHandler(caplog.handler)


class _Connection:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False

    async def recv(self) -> dict:
        return await self.messages.get()

    async def close(self) -> None:
        self.closed = True


class _DispatchConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


@asynccontextmanager
async def _running(supervisor: RuntimeSupervisor):
    conn = _Connection()
    supervisor._conn = conn
    supervisor._generation = supervisor._peer_generation = 1
    supervisor._last_rx = time.monotonic()
    pump = asyncio.create_task(supervisor._pump())
    try:
        yield conn, pump
    finally:
        supervisor._stopping = True
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def _until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_restarts", [0, 1])
@pytest.mark.parametrize("reason", ["runtime worker heartbeat timed out", "worker business loop stalled"])
async def test_health_failure_publishes_restart_decision_before_completing_requests(
    monkeypatch, max_restarts, reason,
) -> None:
    readiness = Readiness()
    readiness.mark_agent_ready()
    supervisor = RuntimeSupervisor(readiness, max_restarts=max_restarts)
    completion = asyncio.get_running_loop().create_future()
    supervisor._pending["dispatch"] = completion
    retiring = asyncio.Event()
    release_retirement = asyncio.Event()
    restarts = []

    async def bring_up() -> None:
        supervisor._conn = _Connection()

    async def pump() -> None:
        supervisor._unhealthy_reason = reason
        raise ConnectionError("worker connection closed")

    async def retire() -> None:
        retiring.set()
        await release_retirement.wait()

    async def schedule_restart(failure_reason) -> bool:
        restarts.append(failure_reason)
        return False

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_pump", pump)
    monkeypatch.setattr(supervisor, "_retire_unhealthy_worker", retire)
    monkeypatch.setattr(supervisor, "_schedule_restart", schedule_restart)
    supervisor._loop_task = asyncio.create_task(supervisor._supervise())
    try:
        await asyncio.wait_for(retiring.wait(), 1)
        assert completion.done() and not completion.result()["ok"]
        expected = ReadinessState.FAILED if max_restarts == 0 else ReadinessState.RUNTIME_WARMING
        assert readiness.state is expected
        assert supervisor._fatal is (max_restarts == 0)
        assert supervisor._ipc_ready.is_set() is (max_restarts == 0)
        assert restarts == [], "replacement must wait until the old Worker retires"
    finally:
        release_retirement.set()
        await asyncio.wait_for(supervisor._loop_task, 1)
    assert restarts == ([reason] if max_restarts else [])


def _pong(beat_age: float | None = None) -> dict:
    message = {"kind": KIND_HEARTBEAT}
    if beat_age is not None:
        message["loop_beat_age"] = beat_age
    return message


# --- Fast path: the reader thread answers pings without the event loop -----


@pytest.mark.asyncio
async def test_fast_path_keeps_reading_after_business_delivery() -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    try:
        conn.set_heartbeat_responder(lambda _m: conn.send_direct(heartbeat_reply(0.5)))
        parent.send_bytes(encode_frame({"kind": KIND_WIRE, "request_id": "first"}))
        assert (await asyncio.wait_for(conn.recv(), 2))["request_id"] == "first"

        # Session initialization can keep the consumer from calling recv again.
        # A business frame before the ping must not retire the heartbeat reader.
        parent.send_bytes(encode_frame({"kind": KIND_WIRE, "request_id": "second"}))
        parent.send_bytes(encode_frame({"kind": KIND_HEARTBEAT}))
        assert await asyncio.to_thread(parent.poll, 0.5), (
            "the heartbeat reader stopped after delivering a business frame"
        )
        pong = conn._decode_pipe_frame(parent.recv_bytes())
        assert pong["kind"] == KIND_HEARTBEAT and pong["loop_beat_age"] == 0.5
        assert (await asyncio.wait_for(conn.recv(), 2))["request_id"] == "second"
    finally:
        await conn.close()
        parent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_pipe_receive_backlog_is_bounded_and_keeps_frame_order(monkeypatch, limit) -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    first = {"kind": KIND_WIRE, "sequence": 0}
    size = len(encode_frame(first))
    if limit == "messages":
        monkeypatch.setattr(stream_module, "_PIPE_PENDING_MESSAGES", 3)
    else:
        monkeypatch.setattr(stream_module, "_PIPE_PENDING_BYTES", 3 * size)
    try:
        parent.send_bytes(encode_frame(first))
        assert (await asyncio.wait_for(conn.recv(), 2))["sequence"] == 0
        for index in range(1, 5):
            parent.send_bytes(encode_frame({"kind": KIND_WIRE, "sequence": index}))
        await _until(lambda: conn._reader_error is not None)
        assert conn.diagnostics()["receive_pending"] == 3
        assert conn.diagnostics()["receive_pending_bytes"] == 3 * size
        assert [(await conn.recv())["sequence"] for _ in range(3)] == [1, 2, 3]
        with pytest.raises(IpcError, match="receive backlog exceeded"):
            await conn.recv()
    finally:
        await conn.close()
        parent.close()
    assert conn.diagnostics()["receive_pending_bytes"] == 0


@pytest.mark.asyncio
async def test_pipe_reader_propagates_protocol_errors_after_pending_frames() -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    try:
        parent.send_bytes(encode_frame({"kind": KIND_WIRE, "sequence": 0}))
        assert (await conn.recv())["sequence"] == 0
        parent.send_bytes(encode_frame({"kind": KIND_WIRE, "sequence": 1}))
        parent.send_bytes(b"broken")
        await _until(lambda: conn._reader_error is not None)
        assert (await conn.recv())["sequence"] == 1
        with pytest.raises(IpcError):
            await conn.recv()
    finally:
        await conn.close()
        parent.close()


@pytest.mark.asyncio
async def test_pipe_close_releases_idle_reader_without_peer_close() -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    receiving = asyncio.create_task(conn.recv())
    try:
        await asyncio.sleep(0.02)
        await conn.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(receiving, 1)
        await _until(lambda: not any(thread.is_alive() for thread in conn._reader_pool._threads))
        assert not parent.closed
    finally:
        await conn.close()
        parent.close()
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)


def test_heartbeat_does_not_postpone_stalled_worker_stack_dump(monkeypatch) -> None:
    clock = SimpleNamespace(now=10.0)
    calls = []
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(diagnostics, "_enabled", True)
    monkeypatch.setattr(diagnostics, "_last_arm", 0.0)
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later", calls.append)
    diagnostics.arm_stall_dump(beat_age=0.0)
    clock.now = 20.0
    diagnostics.arm_stall_dump(beat_age=10.0)
    clock.now = 30.0
    diagnostics.arm_stall_dump(beat_age=20.0)
    assert calls == [25.0]
    diagnostics.arm_stall_dump(beat_age=0.0)
    assert calls == [25.0, 25.0]


@pytest.mark.asyncio
async def test_fast_path_answers_ping_off_loop_and_skips_delivery() -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    try:
        replied: list[tuple[int, dict]] = []

        def respond(message: dict) -> None:
            replied.append((threading.get_ident(), message))
            conn.send_direct(heartbeat_reply(0.25))

        conn.set_heartbeat_responder(respond)
        parent.send_bytes(encode_frame({"kind": KIND_HEARTBEAT}))
        parent.send_bytes(encode_frame({"kind": "wire", "request_id": "r"}))

        delivered = await asyncio.wait_for(conn.recv(), 2)
        assert delivered["kind"] == "wire", "ping must not reach the event loop"

        thread_id, ping = replied[0]
        assert ping["kind"] == KIND_HEARTBEAT
        assert thread_id != threading.get_ident(), "pong left the loop thread"

        pong = conn._decode_pipe_frame(parent.recv_bytes())
        assert pong["kind"] == KIND_HEARTBEAT
        assert pong["loop_beat_age"] == 0.25
    finally:
        await conn.close()
        parent.close()


@pytest.mark.asyncio
async def test_fast_path_direct_send_serializes_with_loop_send() -> None:
    parent, child = Pipe(duplex=True)
    conn = PipeConnection(child)
    try:
        conn.set_heartbeat_responder(lambda _m: conn.send_direct(heartbeat_reply(0.0)))
        parent.send_bytes(encode_frame({"kind": KIND_HEARTBEAT}))
        parent.send_bytes(encode_frame({"kind": "wire", "request_id": "r"}))
        assert (await asyncio.wait_for(conn.recv(), 2))["kind"] == "wire"

        await conn.send({"kind": KIND_WIRE, "request_id": "from-loop"})

        frames = [conn._decode_pipe_frame(parent.recv_bytes()) for _ in range(2)]
        by_id = {str(frame.get("request_id")): frame for frame in frames}
        assert "from-loop" in by_id
        assert any(frame["kind"] == KIND_HEARTBEAT for frame in frames)
    finally:
        await conn.close()
        parent.close()


def test_loop_beat_and_reply_field_semantics(monkeypatch) -> None:
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    beat = LoopBeat()
    clock.now += 0.25
    assert beat.age == 0.25, "a blocked loop must age the beat"
    beat.beat()
    assert beat.age == 0.0

    assert heartbeat_reply(None) == {"kind": KIND_HEARTBEAT, "pid": os.getpid()}
    assert "loop_beat_age" not in heartbeat_reply(None)
    assert heartbeat_reply(1.5)["loop_beat_age"] == 1.5


@pytest.mark.asyncio
async def test_dispatcher_pong_carries_beat_age(monkeypatch) -> None:
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    bridge = WorkerGatewayBridge(ConnHolder())
    beat = LoopBeat()
    clock.now += 0.25
    dispatcher = WorkerDispatcher(bridge, beat=beat)
    conn = _DispatchConn()
    assert await dispatcher.handle({"kind": KIND_HEARTBEAT}, conn)
    assert conn.sent[0]["kind"] == KIND_HEARTBEAT
    assert conn.sent[0]["loop_beat_age"] == 0.25

    legacy = _DispatchConn()
    assert await WorkerDispatcher(bridge).handle({"kind": KIND_HEARTBEAT}, legacy)
    assert "loop_beat_age" not in legacy.sent[0]


# --- Supervisor classification ---------------------------------------------


@pytest.mark.asyncio
async def test_stalled_loop_warns_without_replacing_worker(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(module, "_WATCHDOG_INTERVAL", 0.01)
    monkeypatch.setattr(module, "_WORKER_STALL_WARN", 0.05)
    monkeypatch.setattr(module, "_WORKER_STALL_RESTART", 10.0)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_pong(beat_age=0.5))
        watchdog = asyncio.create_task(supervisor._watchdog(1))
        try:
            await asyncio.sleep(0.2)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        assert not conn.closed, "a transient stall must not replace the Worker"
        assert "worker loop unresponsive" in caplog.text
        assert "business loop stalled" not in caplog.text


@pytest.mark.asyncio
async def test_sustained_stall_replaces_worker(monkeypatch, caplog) -> None:
    monkeypatch.setattr(module, "_WATCHDOG_INTERVAL", 0.01)
    monkeypatch.setattr(module, "_WORKER_STALL_WARN", 0.05)
    monkeypatch.setattr(module, "_WORKER_STALL_RESTART", 0.1)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_pong(beat_age=5.0))
        await asyncio.wait_for(supervisor._watchdog(1), 1)
        assert conn.closed, "a sustained stall must be replaced"
        assert "business loop stalled" in caplog.text
        assert "heartbeat timed out" not in caplog.text, (
            "transport was alive; the verdict must name the real fault"
        )


@pytest.mark.asyncio
async def test_supervisor_self_stall_defers_transport_verdict(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(module, "_WATCHDOG_INTERVAL", 0.01)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        supervisor._last_rx = time.monotonic() - 60
        watchdog = asyncio.create_task(supervisor._watchdog(1))
        try:
            await asyncio.sleep(0)  # watchdog enters its first interval sleep
            time.sleep(0.05)  # this Front's own loop stalls
            # The overdue sleep timer only queues the watchdog. The first
            # yield runs that timer; the second resumes ahead of the verdict
            # and enqueues the missed pong while _last_rx is still stale, so
            # the verdict must defer. The pump applies the pong on the next
            # turn, before the following interval — a 1ms timer instead races
            # that 10ms tick and lets the verdict close the Worker first.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            conn.messages.put_nowait(_pong(beat_age=0.0))
            await asyncio.sleep(0)  # pump refreshes _last_rx
            await asyncio.sleep(0.05)  # later cycles see fresh rx
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        assert not conn.closed, "the Front must not execute the Worker for its own stall"
        assert "supervisor event loop stalled" in caplog.text
        assert "heartbeat timed out" not in caplog.text


@pytest.mark.asyncio
async def test_legacy_pongs_keep_worker_outside_stall_detection(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(module, "_WATCHDOG_INTERVAL", 0.01)
    monkeypatch.setattr(module, "_WORKER_STALL_WARN", 0.05)
    monkeypatch.setattr(module, "_WORKER_STALL_RESTART", 0.1)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        watchdog = asyncio.create_task(supervisor._watchdog(1))
        try:
            for _ in range(8):
                await conn.messages.put(_pong())
                await asyncio.sleep(0.01)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        assert not conn.closed
        assert "worker loop unresponsive" not in caplog.text
        assert "business loop stalled" not in caplog.text


@pytest.mark.asyncio
async def test_business_frame_proves_the_worker_loop_ran() -> None:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_pong(beat_age=30.0))
        stalled = time.monotonic() - 30.0
        await _until(lambda: abs(supervisor._worker_last_beat - stalled) < 1.0)

        await conn.messages.put(
            {
                "kind": KIND_WIRE,
                "generation": 1,
                "request_id": "r1",
                "payload": {"request_id": "r1"},
            }
        )
        await _until(lambda: supervisor._worker_last_beat > stalled + 5.0)
