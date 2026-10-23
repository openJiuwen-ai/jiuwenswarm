# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway backpressure must not prevent Worker health checks."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import (
    KIND_HEARTBEAT,
    KIND_READINESS,
    KIND_REQUEST_DONE,
    KIND_WIRE,
)
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


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
        self.reads = 0
        self.closed = False

    async def recv(self) -> dict:
        message = await self.messages.get()
        self.reads += 1
        return message

    async def close(self) -> None:
        self.closed = True


class _WebSocket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.frames: list[dict] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def send(self, raw: str) -> None:
        self.started.set()
        await self.release.wait()
        self.frames.append(json.loads(raw))


async def _until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _wire(request_id: str, sequence: int, *, generation: int = 1) -> dict:
    return {
        "kind": KIND_WIRE,
        "generation": generation,
        "request_id": request_id,
        "payload": {"request_id": request_id, "sequence": sequence},
    }


def _done(request_id: str) -> dict:
    return {
        "kind": KIND_REQUEST_DONE,
        "generation": 1,
        "request_id": request_id,
        "ok": True,
        "delivered": True,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["lock", "send"])
async def test_blocked_gateway_keeps_reading_heartbeat_and_readiness(
    blocked_at,
) -> None:
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder())
    ws = _WebSocket(blocked=blocked_at == "send")
    lock = asyncio.Lock()
    if blocked_at == "lock":
        await lock.acquire()
    supervisor._routes["r1"] = (ws, lock)
    async with _running(supervisor) as (conn, pump):
        await conn.messages.put(_wire("r1", 0))
        await conn.messages.put({"kind": KIND_HEARTBEAT})
        await conn.messages.put({"kind": KIND_READINESS, "state": "AGENT_READY"})
        await _until(lambda: conn.reads == 3)
        assert readiness.state is ReadinessState.AGENT_READY
        assert not conn.closed and not pump.done()
        assert ws.frames == []
        if blocked_at == "lock":
            lock.release()
        else:
            ws.release.set()
        await _until(lambda: len(ws.frames) == 1)


@pytest.mark.asyncio
async def test_completion_waits_for_ordered_wire_delivery() -> None:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws, lock = _WebSocket(blocked=True), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    future = asyncio.get_running_loop().create_future()
    supervisor._pending["r1"] = future
    async with _running(supervisor) as (conn, _pump):
        for sequence in range(4):
            await conn.messages.put(_wire("r1", sequence))
        await conn.messages.put(_done("r1"))
        await conn.messages.put({"kind": KIND_HEARTBEAT})
        await _until(lambda: conn.reads == 6)
        assert not future.done()
        ws.release.set()
        result = await asyncio.wait_for(future, 1)
        assert result["ok"] is True
        assert [frame["sequence"] for frame in ws.frames] == list(range(4))


@pytest.mark.asyncio
async def test_send_timeout_fails_request_without_worker_restart(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(module, "_FORWARD_TIMEOUT", 0.05, raising=False)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws = _WebSocket(blocked=True)
    supervisor._routes["r1"] = (ws, asyncio.Lock())
    future = asyncio.get_running_loop().create_future()
    supervisor._pending["r1"] = future
    async with _running(supervisor) as (conn, pump):
        await conn.messages.put(_wire("r1", 0))
        await conn.messages.put(_done("r1"))
        result = await asyncio.wait_for(future, 1)
        assert result["ok"] is False and result["delivered"] is False
        assert result["code"] == "GATEWAY_FORWARD_FAILED"
        assert "Gateway" in result["error"]
        assert "timed out" in caplog.text
        assert not conn.closed and not pump.done()
        assert supervisor._restart_count == 0


@pytest.mark.asyncio
async def test_queue_overflow_fails_request_and_keeps_reading(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(module, "_FORWARD_QUEUE_SIZE", 2, raising=False)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws = _WebSocket(blocked=True)
    supervisor._routes["r1"] = (ws, asyncio.Lock())
    future = asyncio.get_running_loop().create_future()
    supervisor._pending["r1"] = future
    async with _running(supervisor) as (conn, pump):
        await conn.messages.put(_wire("r1", 0))
        await asyncio.wait_for(ws.started.wait(), 1)
        for sequence in range(1, 5):
            await conn.messages.put(_wire("r1", sequence))
        await conn.messages.put(_done("r1"))
        await conn.messages.put({"kind": KIND_HEARTBEAT})
        await _until(lambda: conn.reads == 7)
        result = await asyncio.wait_for(future, 1)
        assert result["ok"] is False
        assert "queue is full" in result["error"]
        assert "queue is full" in caplog.text
        assert supervisor._forward_queue.qsize() <= 2
        assert not conn.closed and not pump.done()
        ws.release.set()


@pytest.mark.asyncio
async def test_gateway_reconnect_does_not_reroute_queued_server_push() -> None:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    old_ws, new_ws = _WebSocket(blocked=True), _WebSocket()
    old_lock = asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = old_ws, old_lock
    async with _running(supervisor) as (conn, _pump):
        for sequence in range(2):
            message = _wire("push", sequence)
            message["payload"]["metadata"] = {E2A_WIRE_SERVER_PUSH_KEY: True}
            await conn.messages.put(message)
        await asyncio.wait_for(old_ws.started.wait(), 1)
        await _until(lambda: conn.reads == 2)
        supervisor._gateway_ws, supervisor._gateway_lock = new_ws, asyncio.Lock()
        old_ws.release.set()
        await asyncio.sleep(0.05)
        assert new_ws.frames == []


@pytest.mark.asyncio
async def test_generation_change_cancels_queued_output() -> None:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws = _WebSocket(blocked=True)
    supervisor._routes["r1"] = (ws, asyncio.Lock())
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0, generation=0))
        await conn.messages.put(_wire("r1", 1))
        await conn.messages.put(_wire("r1", 2))
        await asyncio.wait_for(ws.started.wait(), 1)
        await _until(lambda: conn.reads == 3)
        supervisor._generation = 2
        ws.release.set()
        await asyncio.sleep(0.05)
        assert all(frame["sequence"] == 1 for frame in ws.frames)


@pytest.mark.asyncio
async def test_watchdog_still_closes_a_silent_worker(monkeypatch, caplog) -> None:
    monkeypatch.setattr(module, "_WATCHDOG_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(module, "_HEARTBEAT_TIMEOUT", 0.03)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    async with _running(supervisor) as (conn, _pump):
        await asyncio.wait_for(supervisor._watchdog(1), 0.5)
        assert conn.closed
        assert "heartbeat timed out" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_pump_does_not_leave_a_gateway_writer() -> None:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws = _WebSocket(blocked=True)
    supervisor._routes["r1"] = (ws, asyncio.Lock())
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0))
        await asyncio.wait_for(ws.started.wait(), 1)
    assert supervisor._forward_queue is None
    assert not any(
        task.get_name() == "runtime-gateway-forward" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_done_queue_overflow_cannot_report_success(monkeypatch) -> None:
    monkeypatch.setattr(module, "_FORWARD_QUEUE_SIZE", 1)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    ws = _WebSocket(blocked=True)
    supervisor._routes["r1"] = (ws, asyncio.Lock())
    future = asyncio.get_running_loop().create_future()
    supervisor._pending["r1"] = future
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0))
        await asyncio.wait_for(ws.started.wait(), 1)
        await conn.messages.put(_wire("r1", 1))
        await conn.messages.put(_done("r1"))
        result = await asyncio.wait_for(future, 1)
        assert result["ok"] is False
        assert result["code"] == "GATEWAY_FORWARD_FAILED"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_real_worker_and_websocket_survive_gateway_stall(monkeypatch) -> None:
    """Use a subprocess and OS IPC, with production heartbeat/watchdog timing.

    Only the forwarding budget is lengthened so an undelivered request stays
    pending beyond the 30-second Worker timeout. The Worker is the protocol
    stub; this validates transport and lifecycle without a model dependency.
    """
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    monkeypatch.setattr(module, "_FORWARD_TIMEOUT", 45.0)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(
        readiness,
        forwarder=EventForwarder(),
        socket_path=f"/tmp/jw-hb-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock",
        stub=True,
        max_restarts=0,
    )
    attached = asyncio.Event()
    gateway: dict = {}

    async def handler(ws):
        lock = asyncio.Lock()
        gateway.update(ws=ws, lock=lock)
        supervisor.attach_gateway_connection(ws, lock)
        attached.set()
        try:
            await ws.wait_closed()
        finally:
            await supervisor.on_gateway_disconnect(ws, "acceptance")

    def request() -> AgentRequest:
        return AgentRequest(
            request_id=uuid.uuid4().hex,
            channel_id="web",
            session_id="heartbeat-acceptance",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "transport acceptance"},
        )

    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 10)
        assert await readiness.wait_agent_ready(5)
        pid = supervisor.worker_pid
        process = supervisor._process
        assert pid != os.getpid() and process is not None
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as client:
                await asyncio.wait_for(attached.wait(), 2)
                lock = gateway["lock"]
                await lock.acquire()
                pending = asyncio.create_task(
                    supervisor.dispatch_parsed_request(gateway["ws"], request(), lock)
                )
                started = time.monotonic()
                try:
                    await asyncio.sleep(36)
                    assert not pending.done()
                    assert supervisor._last_rx > started + 25
                    assert supervisor.worker_pid == pid and process.returncode is None
                    assert supervisor._restart_count == 0
                    assert readiness.state is ReadinessState.AGENT_READY
                finally:
                    lock.release()
                await asyncio.wait_for(pending, 5)
                wire = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert wire["echo"] == "chat.send"
                assert wire["session_id"] == "heartbeat-acceptance"
            attached.clear()
            async with connect(f"ws://127.0.0.1:{port}") as client:
                await asyncio.wait_for(attached.wait(), 2)
                await asyncio.wait_for(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"], request(), gateway["lock"]
                    ),
                    5,
                )
                assert (
                    json.loads(await asyncio.wait_for(client.recv(), 5))["echo"]
                    == "chat.send"
                )
                assert supervisor.worker_pid == pid and supervisor._restart_count == 0
        print(
            f"REAL_TRANSPORT_ACCEPTANCE pid={pid} stall_seconds=36 "
            "heartbeat_timeout=30 ordered_delivery=ok reconnect=ok restarts=0"
        )
    finally:
        await supervisor.stop(terminate_worker=True)
