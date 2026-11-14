# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Accepted streams survive a resident IPC detach without resubmission."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import (
    KIND_REQUEST, KIND_REQUEST_DONE, KIND_SNAPSHOT, KIND_SUBSCRIBE, KIND_WIRE, dump_agent_request,
)
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_registry import WorkerSlot
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import (
    ConnHolder, WorkerGatewayBridge, begin_request_dispatch, end_request_dispatch,
)


async def _until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class Gateway:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


class Connection:
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send(self, message):
        self.frames.append(message)

    async def close(self):
        self.closed = True


def _request(*, stream=True):
    return AgentRequest(request_id="original", channel_id="web", user_id="owner",
        session_id="inflight-session", req_method=ReqMethod.CHAT_SEND,
        params={"query": "run once"}, is_stream=stream)


def _front(conn):
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    supervisor._conn = supervisor._validated_conn = conn
    supervisor._worker_pid = os.getpid()
    supervisor._worker_create_time = psutil.Process().create_time()
    supervisor._worker_instance = "resident-instance"
    supervisor._peer_generation = supervisor._generation = 1
    supervisor._registry.upsert(WorkerSlot(worker_id="primary", pid=os.getpid(), socket_path="test", ready=True))
    return supervisor


@pytest.mark.asyncio
async def test_delivered_cursor_filters_replayed_duplicates():
    conn = Connection()
    supervisor = _front(conn)
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(), asyncio.Lock()))
    await _until(lambda: bool(conn.frames))
    dispatch_id = conn.frames[0]["dispatch_id"]
    for sequence in [1, 1, 2]:
        await supervisor._handle({"kind": KIND_WIRE, "generation": 1, "dispatch_id": dispatch_id,
                                  "request_id": "original", "sequence": sequence, "wire_id": str(sequence),
                                  "payload": {"request_id": "original", "index": sequence}})
    await supervisor._handle({"kind": KIND_REQUEST_DONE, "generation": 1, "dispatch_id": dispatch_id,
                              "request_id": "original", "ok": True, "delivered": True})
    await pending
    assert [frame["index"] for frame in gateway.frames] == [1, 2]
    assert len(conn.frames) == 4  # One request, plus ACKs including the duplicate.
    assert not supervisor._streams


@pytest.mark.asyncio
async def test_changed_worker_fails_original_without_resubmitting():
    conn = Connection()
    supervisor = _front(conn)
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(), asyncio.Lock()))
    await _until(lambda: bool(conn.frames))
    supervisor._worker_instance = "replacement-instance"
    await supervisor._resume_streams(conn)
    await pending
    assert len(conn.frames) == 1
    assert "changed during stream recovery" in gateway.frames[0]["body"]["details"]["error"]
    assert not supervisor._pending and not supervisor._streams


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("retained", [False, True])
async def test_uncertain_request_send_queries_acceptance_without_resending(close_fails, retained):
    class UncertainConnection(Connection):
        async def send(self, message):
            await super().send(message)
            raise ConnectionError("write outcome unknown")

        async def close(self):
            await super().close()
            if close_fails:
                raise RuntimeError("injected close failure")

    original = UncertainConnection()
    supervisor = _front(original)
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(), asyncio.Lock()))
    await _until(lambda: original.closed)
    assert not pending.done()
    restored = Connection()
    await supervisor._resume_streams(restored)
    assert [frame["kind"] for frame in restored.frames] == [KIND_SUBSCRIBE]
    query = restored.frames[0]
    await supervisor._handle({"kind": KIND_SNAPSHOT, "generation": 1, "query_id": query["query_id"],
        "payload": {"worker_instance": "resident-instance", "dispatch_id": query["dispatch_id"],
                    "recovery_error": "" if retained else "runtime worker no longer retains the original stream"}})
    if retained:
        await supervisor._handle({"kind": KIND_REQUEST_DONE, "generation": 1,
            "dispatch_id": query["dispatch_id"], "request_id": "original", "ok": True, "delivered": True})
    await pending
    assert len(original.frames) == 1
    if retained:
        assert not gateway.frames
    else:
        assert len(gateway.frames) == 1 and gateway.frames[0]["response_kind"] == "e2a.error"
    assert not supervisor._streams and not supervisor._stream_queries


@pytest.mark.asyncio
async def test_stop_resolves_stream_even_when_transport_close_fails():
    class BrokenClose(Connection):
        async def close(self):
            raise RuntimeError("injected close failure")

    conn = BrokenClose()
    supervisor = _front(conn)
    released = asyncio.Event()

    async def side_task():
        try:
            await released.wait()
        finally:
            released.set()

    watcher = asyncio.create_task(side_task())
    loop = asyncio.create_task(released.wait())
    supervisor._side_tasks.append(watcher)
    supervisor._loop_task = loop
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(), asyncio.Lock()))
    try:
        await _until(lambda: bool(conn.frames))
        with pytest.raises(RuntimeError, match="injected close failure"):
            await supervisor.stop()
        await pending
        assert "worker stopped" in gateway.frames[0]["body"]["details"]["error"]
        assert not supervisor._pending and not supervisor._streams
        assert watcher.done() and loop.done() and not supervisor._side_tasks
    finally:
        for task in [watcher, loop, pending]:
            task.cancel()
        await asyncio.gather(watcher, loop, pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_supervisor_resolves_its_pending_stream(monkeypatch):
    conn = Connection()
    supervisor = _front(conn)
    entered = asyncio.Event()

    async def bring_up():
        return None

    async def pump():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_pump", pump)
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(), asyncio.Lock()))
    loop = asyncio.create_task(supervisor._supervise())
    supervisor._loop_task = loop
    try:
        await entered.wait()
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await asyncio.sleep(0)
        assert pending.done()
        await pending
        assert gateway.frames[0]["body"]["details"]["code"] == "WORKER_CLOSED"
        assert not supervisor._pending and not supervisor._streams
    finally:
        pending.cancel()
        loop.cancel()
        await asyncio.gather(pending, loop, return_exceptions=True)
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("fail_at", [0, 1])
async def test_multiple_streams_retry_failed_subscription_without_resubmission(close_fails, fail_at):
    class BrokenSubscription(Connection):
        async def send(self, message):
            await super().send(message)
            if len(self.frames) == fail_at + 1:
                raise ConnectionError("subscription write outcome unknown")

        async def close(self):
            await super().close()
            if close_fails:
                raise RuntimeError("subscription transport close failed")

    original = Connection()
    supervisor = _front(original)
    pending = [asyncio.create_task(supervisor.dispatch_parsed_request(
        Gateway(), replace(_request(), request_id=f"request-{index}", session_id=f"session-{index}"),
        asyncio.Lock())) for index in range(2)]
    try:
        await _until(lambda: len(original.frames) == 2)
        for dispatch_id, stream in supervisor._streams.items():
            supervisor._begin_stream_recovery(dispatch_id, stream)
        broken = BrokenSubscription()
        supervisor._conn = supervisor._validated_conn = broken
        await supervisor._resume_streams_connected(broken)
        assert broken.closed and all(not task.done() for task in pending)
        restored = Connection()
        supervisor._conn = supervisor._validated_conn = restored
        await supervisor._resume_streams_connected(restored)
        assert len(restored.frames) == 2
        assert all(frame["kind"] == KIND_SUBSCRIBE for frame in restored.frames)
        for frame in restored.frames:
            await supervisor._handle({"kind": KIND_SNAPSHOT, "generation": 1, "query_id": frame["query_id"],
                "payload": {"worker_instance": "resident-instance", "dispatch_id": frame["dispatch_id"],
                            "recovery_error": ""}})
            await supervisor._handle({"kind": KIND_REQUEST_DONE, "generation": 1,
                "dispatch_id": frame["dispatch_id"], "ok": True, "delivered": True})
        await asyncio.gather(*pending)
        assert len(original.frames) == 2
        assert not supervisor._pending and not supervisor._streams and not supervisor._stream_queries
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        supervisor._conn = None
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ack", "nack", "detached"])
async def test_critical_reply_in_stream_dispatch_keeps_delivery_semantics(outcome):
    holder = ConnHolder()
    holder.open_session(Connection())
    bridge = WorkerGatewayBridge(holder)
    record = bridge.events.register(_request(), "stream-dispatch")
    record.delivery_generation = holder.generation
    restored = Connection()
    holder.open_session(restored)
    if outcome == "detached":
        holder.close_session()
    token = begin_request_dispatch(record.dispatch_id)
    pending = asyncio.create_task(bridge.send(json.dumps({"request_id": "original", "control": "reply"})))
    try:
        if outcome == "detached":
            with pytest.raises(ConnectionError, match="no Gateway delivery path"):
                await pending
        else:
            await _until(lambda: bool(restored.frames) or pending.done())
            assert restored.frames and not pending.done()
            frame = restored.frames[0]
            assert frame["requires_ack"] is True
            bridge.acknowledge({"wire_id": frame["wire_id"], "generation": holder.generation,
                                "delivered": outcome == "ack"})
            if outcome == "ack":
                await pending
            else:
                with pytest.raises(ConnectionError, match="could not deliver"):
                    await pending
        assert not bridge.events.replay(_request(), after=0) and not bridge._acks
    finally:
        end_request_dispatch(token)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_push", [False, True])
async def test_replayable_frames_wait_for_subscription(server_push):
    holder = ConnHolder()
    holder.open_session(Connection())
    bridge = WorkerGatewayBridge(holder)
    record = bridge.events.register(_request(), "stream-dispatch")
    record.delivery_generation = holder.generation
    restored = Connection()
    holder.open_session(restored)
    payload = {"request_id": "original", "index": 0}
    if server_push:
        payload["metadata"] = {E2A_WIRE_SERVER_PUSH_KEY: True}
    else:
        payload["is_stream"] = True
    token = begin_request_dispatch(record.dispatch_id)
    try:
        await bridge.send(json.dumps(payload))
    finally:
        end_request_dispatch(token)
    assert not restored.frames
    assert len(bridge.events.replay(_request(), after=0)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["completed", "other_scope_eviction", "expired", "user", "session", "instance", "missing"])
async def test_worker_replay_validates_owner_identity_and_cursor(case):
    holder = ConnHolder()
    old = Connection()
    holder.open_session(old)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    started, release = asyncio.Event(), asyncio.Event()

    class Runtime:
        calls = 0

        async def dispatch_parsed_request(self, ws, request, lock):
            self.calls += 1
            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                      "is_complete": False, "index": 0}))
            started.set()
            await release.wait()
            for index in range(1, 7):
                await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                          "is_complete": index == 6, "index": index}))

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    await dispatcher.mark_ready()
    request = _request()
    await dispatcher.handle({"kind": KIND_REQUEST, "generation": 1, "dispatch_id": "dispatch",
                             "payload": dump_agent_request(request)}, old)
    await asyncio.wait_for(started.wait(), 1)
    holder.close_session()
    await dispatcher.retire_generation(1)
    if case == "expired":
        bridge.events._max_events = 2
    elif case == "other_scope_eviction":
        bridge.events._max_events = 8
        other = replace(request, request_id="other", user_id="other-owner")
        bridge.events.register(other, "other-dispatch")
        for index in range(20):
            bridge.events.append({"request_id": "other", "is_stream": True, "index": index}, "other-dispatch")
        bridge.events.finish("other-dispatch")
    release.set()
    await _until(lambda: bridge.events.records["dispatch"].completion is not None)
    new = Connection()
    holder.open_session(new)
    if case == "user":
        request = replace(request, user_id="wrong-owner")
    elif case == "session":
        request = replace(request, session_id="wrong-session")
    message = {"kind": KIND_SUBSCRIBE, "generation": 2, "query_id": "recover",
               "dispatch_id": "missing" if case == "missing" else "dispatch",
               "payload": dump_agent_request(request), "after": 1,
               "worker_instance": "wrong-instance" if case == "instance" else bridge.events.instance}
    await dispatcher.handle(message, new)
    await _until(lambda: any(frame["kind"] == KIND_SNAPSHOT for frame in new.frames))
    snapshot = next(frame["payload"] for frame in new.frames if frame["kind"] == KIND_SNAPSHOT)
    if case in {"completed", "other_scope_eviction"}:
        await _until(lambda: any(frame["kind"] == KIND_REQUEST_DONE for frame in new.frames))
        assert not snapshot["recovery_error"]
        assert [frame["payload"]["index"] for frame in new.frames if frame["kind"] == KIND_WIRE] == list(range(1, 7))
        assert new.frames[-1]["ok"] is True and new.frames[-1]["generation"] == 2
    else:
        assert snapshot["recovery_error"]
        assert not any(frame["kind"] in {KIND_WIRE, KIND_REQUEST_DONE} for frame in new.frames)
    assert runtime.calls == 1
    await dispatcher.drain()


@pytest.mark.asyncio
async def test_session_subscription_reenables_original_live_stream():
    holder = ConnHolder()
    first = Connection()
    holder.open_session(first)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    started, release = asyncio.Event(), asyncio.Event()

    class Runtime:
        calls = 0

        async def dispatch_parsed_request(self, ws, request, lock):
            self.calls += 1
            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True, "index": 0}))
            started.set()
            await release.wait()
            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True, "index": 1}))

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    await dispatcher.mark_ready()
    request = _request()
    await dispatcher.handle({"kind": KIND_REQUEST, "generation": 1, "dispatch_id": "dispatch",
                             "payload": dump_agent_request(request)}, first)
    await asyncio.wait_for(started.wait(), 1)
    await dispatcher.retire_generation(holder.close_session())
    second = Connection()
    holder.open_session(second)
    # A new Front's ordinary session restoration still works without owning
    # the original dispatch waiter or using the new per-dispatch resume path.
    await dispatcher.handle({"kind": KIND_SUBSCRIBE, "generation": 2, "query_id": "session",
                             "payload": dump_agent_request(request), "after": 1, "subscribe": True,
                             "worker_instance": bridge.events.instance}, second)
    await _until(lambda: any(frame["kind"] == KIND_SNAPSHOT for frame in second.frames))
    release.set()
    await _until(lambda: any(frame["kind"] == KIND_REQUEST_DONE for frame in second.frames))
    assert [frame["payload"]["index"] for frame in second.frames if frame["kind"] == KIND_WIRE] == [1]
    assert runtime.calls == 1
    await dispatcher.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "unhealthy", "attached", "unary", "timeout", "exhausted", "stop", "cancel"])
async def test_front_recovery_has_terminal_failure_and_cleanup(monkeypatch, failure):
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    conn = Connection()
    supervisor._conn = supervisor._validated_conn = conn
    supervisor._worker_pid = os.getpid()
    supervisor._worker_create_time = psutil.Process().create_time()
    supervisor._worker_instance = "resident-instance"
    supervisor._worker_reached_ready = True
    supervisor._registry.upsert(WorkerSlot(worker_id="primary", pid=os.getpid(), socket_path="test", ready=True))
    supervisor._peer_generation = supervisor._generation = 1
    retired = []

    async def retire():
        retired.append(True)

    async def shutdown(*, deadline):
        pass

    async def bring_up():
        pass

    async def pump():
        raise ConnectionError("injected IPC detach")

    recovering, release = asyncio.Event(), asyncio.Event()

    async def schedule(reason):
        recovering.set()
        if failure == "exhausted":
            supervisor._fatal = True
            supervisor._fatal_reason = "runtime worker restart budget exhausted"
        else:
            await release.wait()
        return False

    monkeypatch.setattr(supervisor, "_bring_up", bring_up)
    monkeypatch.setattr(supervisor, "_pump", pump)
    monkeypatch.setattr(supervisor, "_schedule_restart", schedule)
    monkeypatch.setattr(supervisor, "_retire_unhealthy_worker", retire)
    monkeypatch.setattr(supervisor, "_shutdown_owned_worker", shutdown)
    monkeypatch.setattr(supervisor, "_log_diagnostics", lambda reason: None)
    monkeypatch.setattr(supervisor, "_confirmed_worker_exit", lambda: failure == "exit")
    monkeypatch.setattr(supervisor_module, "_INFLIGHT_RECOVERY_TIMEOUT", 0.05 if failure == "timeout" else 10)
    if failure == "attached":
        supervisor._ipc_mode = supervisor_module.FRONT_LOOPBACK
    elif failure == "unhealthy":
        supervisor._unhealthy_reason = "worker business loop stalled"
    gateway = Gateway()
    pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, _request(stream=failure != "unary"), asyncio.Lock()))
    await _until(lambda: bool(conn.frames))
    loop = asyncio.create_task(supervisor._supervise())
    supervisor._loop_task = loop
    try:
        await asyncio.wait_for(recovering.wait(), 2)
        if failure in {"stop", "cancel", "timeout"}:
            assert not pending.done()
        if failure == "stop":
            await supervisor.stop()
        elif failure == "cancel":
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if failure != "cancel":
            await asyncio.wait_for(asyncio.shield(pending), 2)
        if failure == "cancel":
            assert not gateway.frames
        else:
            assert len(gateway.frames) == 1
            error = gateway.frames[0]["body"]["details"]
            assert error["code"] == "WORKER_CLOSED"
            if failure == "timeout":
                assert "recovery timed out" in error["error"]
        assert not supervisor._pending and not supervisor._routes and not supervisor._streams
        assert len([frame for frame in conn.frames if frame["kind"] == KIND_REQUEST]) == 1
        assert bool(retired) is (failure in {"unhealthy", "attached"})
    finally:
        release.set()
        await asyncio.gather(loop, return_exceptions=True)
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.parametrize(("finish_offline", "delayed_resume", "uncertain_send"), [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True),
])
async def test_real_worker_resumes_original_stream_once(
    monkeypatch, tmp_path, finish_offline, delayed_resume, uncertain_send,
):
    source = Path(supervisor_module.__file__).resolve().parents[3]
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from inflight_stream_runtime import configure\n"
        "    configure()\n", encoding="utf-8",
    )
    paths = [str(bootstrap), str(Path(__file__).parent / "fixtures"), str(source)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("INFLIGHT_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "resident")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder(),
        socket_path=str(tmp_path / "runtime.pipe"))
    reconnect = asyncio.Event()
    detached = asyncio.Event()
    schedule = supervisor._schedule_restart

    async def delay_reconnect(reason):
        detached.set()
        await reconnect.wait()
        return await schedule(reason)

    monkeypatch.setattr(supervisor, "_schedule_restart", delay_reconnect)
    if delayed_resume:
        resume = supervisor._resume_streams

        async def slow_resume(conn):
            send = conn.send

            async def slow_send(message):
                await send(message)
                if message.get("kind") == KIND_SUBSCRIBE:
                    await asyncio.sleep(0.7)

            conn.send = slow_send
            try:
                await resume(conn)
            finally:
                conn.send = send

        monkeypatch.setattr(supervisor, "_resume_streams", slow_resume)
    gateway = Gateway()
    pending = None
    pid = None
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 25)
        assert await readiness.wait_agent_ready(15)
        pid = supervisor.worker_pid
        created = psutil.Process(pid).create_time()
        instance = supervisor.worker_instance
        if uncertain_send:
            original_conn = supervisor._conn
            send = original_conn.send
            close = original_conn.close

            async def uncertain_write(message):
                await send(message)
                if message.get("kind") == KIND_REQUEST:
                    await _until(lambda: sum("index" in frame for frame in gateway.frames) == 5)
                    raise ConnectionError("injected uncertain request send")

            async def broken_close():
                await close()
                raise RuntimeError("injected failed transport close")

            monkeypatch.setattr(original_conn, "send", uncertain_write)
            monkeypatch.setattr(original_conn, "close", broken_close)
        frame_count = 600 if delayed_resume else 40
        request = AgentRequest(request_id="original", channel_id="web", user_id="owner",
            session_id="inflight-session", req_method=ReqMethod.CHAT_SEND,
            params={"query": "run exactly once", "frames": frame_count}, is_stream=True)
        pending = asyncio.create_task(supervisor.dispatch_parsed_request(gateway, request, asyncio.Lock()))
        await _until(lambda: sum("index" in frame for frame in gateway.frames) == 5)
        old_generation = supervisor._generation
        if not uncertain_send:
            await supervisor._conn.close()
        await asyncio.wait_for(detached.wait(), 5)
        # This is the bug's externally visible invariant, before any replay.
        assert not pending.done(), gateway.frames
        (tmp_path / "release-4").touch()
        await _until(lambda: (tmp_path / "at-19").exists())
        if finish_offline:
            (tmp_path / "release-19").touch()
            await _until(lambda: (tmp_path / "finished").exists())
        reconnect.set()
        await _until(lambda: supervisor._generation > old_generation)
        if not finish_offline:
            await _until(lambda: sum("index" in frame for frame in gateway.frames) == 20)
            assert not pending.done()
            (tmp_path / "release-19").touch()
        await asyncio.wait_for(pending, 10)
        assert [frame["index"] for frame in gateway.frames if "index" in frame] == list(range(frame_count))
        assert sum(frame.get("phase") == "final" for frame in gateway.frames) == 1
        assert not any(frame.get("response_kind") == "e2a.error" for frame in gateway.frames)
        executions = (tmp_path / "executions.jsonl").read_text("utf-8").splitlines()
        assert len(executions) == 1
        assert supervisor.worker_pid == pid and supervisor.worker_instance == instance
        assert supervisor._generation == old_generation + 1
        assert psutil.Process(pid).create_time() == created
        assert not supervisor._pending and not supervisor._routes and not supervisor._streams
    finally:
        reconnect.set()
        for index in [4, 19]:
            (tmp_path / f"release-{index}").touch()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await supervisor.stop(terminate_worker=True)
    assert pid is None or not psutil.pid_exists(pid)
