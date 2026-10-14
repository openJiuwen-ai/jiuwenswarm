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

import pytest

from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import KIND_HEARTBEAT, KIND_WIRE, encode_frame
from jiuwenswarm.server.ipc.stream import PipeConnection
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker.diagnostics import LoopBeat, heartbeat_reply
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


def _pong(beat_age: float | None = None) -> dict:
    message = {"kind": KIND_HEARTBEAT}
    if beat_age is not None:
        message["loop_beat_age"] = beat_age
    return message


# --- Fast path: the reader thread answers pings without the event loop -----


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


def test_loop_beat_and_reply_field_semantics() -> None:
    beat = LoopBeat()
    time.sleep(0.05)
    assert beat.age >= 0.05, "a blocked loop must age the beat"
    beat.beat()
    assert beat.age < 0.05

    assert heartbeat_reply(None) == {"kind": KIND_HEARTBEAT, "pid": os.getpid()}
    assert "loop_beat_age" not in heartbeat_reply(None)
    assert heartbeat_reply(1.5)["loop_beat_age"] == 1.5


@pytest.mark.asyncio
async def test_dispatcher_pong_carries_beat_age() -> None:
    bridge = WorkerGatewayBridge(ConnHolder())
    beat = LoopBeat()
    time.sleep(0.05)
    dispatcher = WorkerDispatcher(bridge, beat=beat)
    conn = _DispatchConn()
    assert await dispatcher.handle({"kind": KIND_HEARTBEAT}, conn)
    assert conn.sent[0]["kind"] == KIND_HEARTBEAT
    assert conn.sent[0]["loop_beat_age"] >= 0.05

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
            time.sleep(0.05)  # this Front's own loop stalls; pongs pile up unread
            # The expired watchdog timer fires before this shorter sleep ends,
            # so the first verdict sees stale rx AND high loop lag: defer.
            await asyncio.sleep(0.001)
            await conn.messages.put(_pong(beat_age=0.0))
            await asyncio.sleep(0.05)  # pump drains; later cycles see fresh rx
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
