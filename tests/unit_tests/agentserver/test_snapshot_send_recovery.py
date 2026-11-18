# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Snapshot observers cannot cancel a shared native IPC write."""

from __future__ import annotations

import asyncio
import os
from multiprocessing.connection import Pipe

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front import router
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.ipc.protocol import decode_frame
from jiuwenswarm.server.ipc.stream import PipeConnection
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


def request(session="a", **params):
    return AgentRequest(request_id=f"metadata-{session}", channel_id="web", user_id="owner",
                        session_id=session, req_method=ReqMethod.SESSION_GET_METADATA,
                        params={"session_id": session, **params})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class DelayedConnection:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.frames = []
        self.cancelled = False
        self.closed = False
        self.fail = False

    async def send(self, frame):
        self.frames.append(frame)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            await self.close()
            raise
        if self.fail:
            raise ConnectionError("test write failed")
        future = self.supervisor._queries.get(frame["query_id"])
        if future is not None and not future.done():
            future.set_result({"is_processing": False, "session": frame["payload"]["session_id"]})

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_snapshot_waits_for_validated_ipc_identity():
    supervisor = RuntimeSupervisor(Readiness())
    conn = DelayedConnection(supervisor)
    supervisor._conn = conn
    assert (await supervisor.get_runtime_snapshot(request()))["recovery_pending"]
    assert not conn.frames and not supervisor._side_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_observer", [False, True])
async def test_observer_timeout_or_cancel_preserves_one_owned_write(monkeypatch, cancel_observer):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.02)
    supervisor = RuntimeSupervisor(Readiness())
    conn = DelayedConnection(supervisor)
    supervisor._conn = conn
    supervisor._validated_conn = conn
    observing = asyncio.create_task(supervisor.get_runtime_snapshot(request()))
    try:
        await conn.started.wait()
        if cancel_observer:
            observing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observing
        else:
            assert (await observing)["recovery_reason"] == "snapshot_transport_timeout"
        assert not conn.cancelled and not conn.closed
        assert supervisor._queries == {}
        assert (await supervisor.get_runtime_snapshot(request("b")))["recovery_pending"]
        assert len(conn.frames) == 1
        conn.release.set()
        await until(lambda: supervisor._snapshot_send_task is None)
        assert not supervisor._side_tasks
        assert await supervisor.get_runtime_snapshot(request("b")) == {"is_processing": False, "session": "b"}
    finally:
        await supervisor._cancel_side_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("through_router", [False, True])
async def test_owner_cancellation_returns_pending_to_observer(monkeypatch, through_router):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 1)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness)
    conn = DelayedConnection(supervisor)
    supervisor._conn = conn
    supervisor._validated_conn = conn
    replies = []
    if through_router:
        admission = ExecutionAdmission(readiness)
        admission.attach_backend(supervisor)
        method_router = router.MethodRouter(readiness, admission)

        async def metadata(accepted):
            return AgentResponse(request_id=accepted.request_id, channel_id="web", ok=True,
                                 payload={"session_id": "a", "is_processing": True, "status": "processing"})

        async def capture(_ws, _lock, payload):
            replies.append(payload)

        monkeypatch.setattr(router, "_load_session_service", lambda: None)
        monkeypatch.setattr(router, "_HANDLE_SESSION", metadata)
        monkeypatch.setattr(method_router, "_send", capture)
        observing = asyncio.create_task(method_router.dispatch(None, request(), asyncio.Lock()))
    else:
        observing = asyncio.create_task(supervisor.get_runtime_snapshot(request()))
    try:
        await asyncio.wait_for(conn.started.wait(), 1)
        await supervisor._cancel_side_tasks()
        result = await asyncio.wait_for(observing, 1)
        if through_router:
            assert len(replies) == 1
            result = replies[0]["body"]["result"]
            assert result["is_processing"] is True
            assert result["status"] == "processing"
            result = result["runtime"]
        assert result["recovery_pending"] is True
        assert result["recovery_reason"] == "snapshot_transport_cancelled"
        assert not observing.cancelled()
        assert conn.cancelled and conn.closed
        assert not supervisor._queries and not supervisor._side_tasks
        assert supervisor._snapshot_send_task is None
    finally:
        observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)
        await supervisor._cancel_side_tasks()


@pytest.mark.asyncio
async def test_connection_retirement_cleans_up_timed_out_write(monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.01)
    supervisor = RuntimeSupervisor(Readiness())
    conn = DelayedConnection(supervisor)
    supervisor._conn = conn
    supervisor._validated_conn = conn
    assert (await supervisor.get_runtime_snapshot(request()))["recovery_pending"]
    await supervisor._cancel_side_tasks()
    assert conn.cancelled and conn.closed
    assert supervisor._snapshot_send_task is None
    assert not supervisor._side_tasks and not supervisor._queries


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced", [False, True])
async def test_late_write_failure_is_observed_without_closing_replacement(monkeypatch, replaced, caplog):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.01)
    supervisor = RuntimeSupervisor(Readiness())
    conn = DelayedConnection(supervisor)
    supervisor._conn = conn
    supervisor._validated_conn = conn
    try:
        assert (await supervisor.get_runtime_snapshot(request()))["recovery_pending"]
        replacement = DelayedConnection(supervisor)
        if replaced:
            supervisor._conn = replacement
        conn.fail = True
        conn.release.set()
        await until(lambda: supervisor._snapshot_send_task is None)
        assert conn.closed is (not replaced)
        assert not replacement.closed
        assert not supervisor._side_tasks and not supervisor._queries
        assert "runtime snapshot write failed: ConnectionError" in caplog.text
    finally:
        await supervisor._cancel_side_tasks()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows native pipe cancellation regression")
async def test_native_pipe_backpressure_does_not_break_following_request(monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.03)
    supervisor = RuntimeSupervisor(Readiness())
    raw_front, raw_peer = Pipe(duplex=True)
    conn = PipeConnection(raw_front)
    supervisor._conn = conn
    supervisor._validated_conn = conn
    try:
        state = await supervisor.get_runtime_snapshot(request(padding="x" * (256 * 1024)))
        assert state["recovery_reason"] == "snapshot_transport_timeout"
        assert not conn._closed
        frame = await asyncio.wait_for(asyncio.to_thread(raw_peer.recv_bytes), 2)
        assert decode_frame(frame[4:])["kind"] == "execution.subscribe"
        await until(lambda: supervisor._snapshot_send_task is None)
        await conn.send({"kind": "request", "request_id": "models-after-timeout"})
        frame = await asyncio.wait_for(asyncio.to_thread(raw_peer.recv_bytes), 2)
        assert decode_frame(frame[4:])["request_id"] == "models-after-timeout"
    finally:
        raw_peer.close()
        await conn.close()
        await supervisor._cancel_side_tasks()
