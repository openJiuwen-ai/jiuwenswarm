# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scoped recovery and explicit pending state, including real subprocess IPC."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front import router
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.ipc.client import connect_with_retry
from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_REQUEST, KIND_SHUTDOWN, KIND_SHUTDOWN_DONE, dump_agent_request, request_scope
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge, begin_request_dispatch, end_request_dispatch


def request(session="a", **kwargs):
    return AgentRequest(request_id=f"metadata-{session}", channel_id="web", user_id="owner",
                        session_id=session, req_method=ReqMethod.SESSION_GET_METADATA,
                        params={"session_id": session, **kwargs})


@pytest.mark.asyncio
async def test_snapshot_wait_retries_the_same_query_without_duplicate_subscriptions(monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.02)
    supervisor = RuntimeSupervisor(Readiness())
    frames = []

    class Connection:
        async def send(self, frame):
            frames.append(frame)
            asyncio.get_running_loop().call_later(0.03, supervisor._queries[frame["query_id"]].set_result,
                                                  {"is_processing": True, "cursor": 7})

    supervisor._conn = Connection()
    assert await supervisor.get_runtime_snapshot(request()) == {"is_processing": True, "cursor": 7}
    assert len(frames) == 1
    assert supervisor._queries == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_send", [False, True])
async def test_snapshot_timeout_is_bounded_and_explicit(monkeypatch, blocked_send):
    monkeypatch.setattr(runtime_supervisor, "_SNAPSHOT_WAIT_SECONDS", 0.01)
    supervisor = RuntimeSupervisor(Readiness())

    class Connection:
        async def send(self, _frame):
            if blocked_send:
                await asyncio.Event().wait()

    supervisor._conn = Connection()
    before = time.perf_counter()
    state = await supervisor.get_runtime_snapshot(request())
    assert time.perf_counter() - before < 0.25
    assert state["recovery_pending"] is True
    assert state["recovery_reason"] == ("snapshot_transport_timeout" if blocked_send else "snapshot_timeout")
    assert "is_processing" not in state
    assert supervisor._queries == {}


@pytest.mark.asyncio
async def test_disconnected_worker_returns_pending_recovery():
    state = await RuntimeSupervisor(Readiness()).get_runtime_snapshot(request())
    assert state["recovery_pending"] is True
    assert state["recovery_reason"] == "worker_disconnected"


@pytest.mark.asyncio
@pytest.mark.parametrize("processing", [True, False])
async def test_metadata_keeps_processing_state_when_runtime_recovery_is_pending(monkeypatch, processing):
    readiness = Readiness()
    admission = ExecutionAdmission(readiness)
    admission.attach_backend(RuntimeSupervisor(readiness))
    method_router = router.MethodRouter(readiness, admission)
    replies = []

    async def metadata(accepted):
        return AgentResponse(request_id=accepted.request_id, channel_id="web", ok=True,
                             payload={"session_id": "a", "is_processing": processing})

    async def capture(_ws, _lock, payload):
        replies.append(payload)

    monkeypatch.setattr(router, "_load_session_service", lambda: None)
    monkeypatch.setattr(router, "_HANDLE_SESSION", metadata)
    monkeypatch.setattr(method_router, "_send", capture)
    await method_router.dispatch(None, request(), asyncio.Lock())
    payload = replies[0]["body"]["result"]
    assert payload["is_processing"] is processing
    assert payload["runtime"]["recovery_pending"] is True


@pytest.mark.asyncio
async def test_server_push_waits_for_target_scope_recovery_but_source_remains_free():
    holder = ConnHolder()
    frames = []

    class Connection:
        async def send(self, frame):
            frames.append(frame)

    holder.open_session(Connection())
    bridge = WorkerGatewayBridge(holder)
    source = request()
    bridge.events.register(source, "source")
    target = replace(source, session_id="b", params={"session_id": "b"})
    token = begin_request_dispatch("source")
    try:
        async with bridge.publication_lock(request_scope(target)):
            push = asyncio.create_task(bridge.send(json.dumps({"request_id": "push-b", "session_id": "b",
                                "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True}, "body": {"delta": "target"}})))
            await asyncio.sleep(0)
            assert not push.done()
            async with bridge.publication_lock(request_scope(source)):
                pass
            assert frames == []
        await push
        assert frames[0]["payload"]["session_id"] == "b"
        assert bridge.events.replay(source, after=0) == []
    finally:
        end_request_dispatch(token)


@pytest.mark.asyncio
async def test_other_scope_eviction_during_snapshot_send_does_not_lose_captured_replay():
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    bridge.events._max_events = 2
    bridge.events.register(request(), "a")
    bridge.events.register(request("b"), "b")
    for _ in range(2):
        bridge.events.append({"is_stream": True, "body": {"delta": "a"}}, "a")
    frames = []

    class Connection:
        async def send(self, frame):
            frames.append(frame)
            if frame.get("kind") == "execution.snapshot":
                for _ in range(3):
                    bridge.events.append({"is_stream": True, "body": {"delta": "b"}}, "b")
                await asyncio.sleep(0)

    conn = Connection()
    generation = holder.open_session(conn)
    await WorkerDispatcher(bridge)._subscribe({"payload": dump_agent_request(request()), "subscribe": True},
                                            conn, generation)
    assert [frame["sequence"] for frame in frames if frame.get("replayed")] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_persistent_subprocess_recovers_two_sessions_while_replaying_2048_frames(tmp_path):
    root = Path(__file__).resolve().parents[3]
    fixture = Path(__file__).parent / "fixtures" / "worker_recovery_process.py"
    path = str(tmp_path / "worker.sock") if os.name == "nt" else f"/tmp/jw-recovery-{uuid.uuid4().hex[:12]}.sock"
    env = {**os.environ, "PYTHONPATH": str(root)}
    # Keep the test environment's dependencies; a Windows venv launcher may
    # report a different PID from the interpreter serving the IPC connection.
    process = await asyncio.create_subprocess_exec(sys.executable, str(fixture), path, cwd=root, env=env,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    conn = None
    reader_task = None
    gateway_task = None
    try:
        first = await connect_with_retry(path, timeout=10)
        original = await asyncio.wait_for(first.recv(), 3)
        assert original["kind"] == KIND_HELLO
        assert Path(original["python_prefix"]).resolve() == Path(sys.prefix).resolve()
        worker = psutil.Process(original["pid"])
        assert process.pid == worker.pid or process.pid in {parent.pid for parent in worker.parents()}
        await first.close()
        conn = await connect_with_retry(path, timeout=10)
        resumed = await asyncio.wait_for(conn.recv(), 3)
        assert resumed["pid"] == worker.pid == original["pid"]
        assert resumed["instance"] == original["instance"]
        assert resumed["generation"] == original["generation"] + 1
        wires = []
        incoming = []
        complete = asyncio.Event()
        first_replay = asyncio.Event()

        class Capture:
            async def send(self, payload, **_kwargs):
                wires.append(payload)
                if payload.get("body", {}).get("content") == "live-after-replay":
                    complete.set()
                return True

        supervisor = RuntimeSupervisor(Readiness(), forwarder=Capture())
        supervisor._conn = conn
        supervisor._peer_generation = resumed["generation"]
        supervisor._worker_instance = resumed["instance"]
        supervisor._gateway_ws = object()
        supervisor._gateway_lock = asyncio.Lock()
        supervisor._forward_queue = asyncio.Queue(maxsize=256)
        gateway_task = asyncio.create_task(
            supervisor._forward_messages(supervisor._forward_queue, supervisor._generation)
        )

        async def receive():
            while True:
                frame = await conn.recv()
                incoming.append(frame)
                if frame.get("replayed"):
                    first_replay.set()
                if frame["kind"] == KIND_SHUTDOWN_DONE:
                    assert frame["ok"] is True
                    return
                await supervisor._handle(frame)

        reader_task = asyncio.create_task(receive())
        start = time.perf_counter()
        a_state = await supervisor.get_runtime_snapshot(request(runtime_resume={"after": 0, "worker_instance": resumed["instance"]}))
        assert a_state["cursor"] == 2048
        await asyncio.wait_for(first_replay.wait(), 3)
        b_start = time.perf_counter()
        b_state = await supervisor.get_runtime_snapshot(request("b"))
        b_elapsed = time.perf_counter() - b_start
        assert b_elapsed < 0.3
        assert b_state["is_processing"] is True
        assert b_state["executions"][0]["state"] == "waiting_for_control"
        assert b_state["dispatches"][0]["pending_interactions"][0]["interaction_id"] == "b-control"
        replay_count_at_b = sum(bool(frame.get("replayed")) for frame in incoming)
        assert replay_count_at_b < 2048
        live = replace(request(), request_id="live-a", req_method=ReqMethod.CHAT_SEND, is_stream=True)
        await conn.send({"kind": KIND_REQUEST, "generation": resumed["generation"],
                         "dispatch_id": "live-a-dispatch", "payload": dump_agent_request(live)})
        pending_start = time.perf_counter()
        pending = await supervisor.get_runtime_snapshot(request())
        pending_elapsed = time.perf_counter() - pending_start
        assert pending["recovery_pending"] is True
        assert pending["recovery_reason"] == "snapshot_timeout"
        assert pending_elapsed < 1.8
        await asyncio.wait_for(complete.wait(), 20)
        await supervisor._forward_queue.join()
        a_wires = [wire for wire in wires if wire.get("session_id") == "a"]
        sequences = [wire["metadata"]["runtime_event"]["sequence"] for wire in a_wires]
        assert sequences == list(range(1, 2050))
        assert a_wires[-1]["body"]["content"] == "live-after-replay"
        assert sum(bool(frame.get("replayed")) for frame in incoming) == 2048
        final = await supervisor.get_runtime_snapshot(request())
        assert final["cursor"] == 2049
        assert final["is_processing"] is True
        evidence = {"worker_pid": worker.pid, "launcher_pid": process.pid, "python_prefix": original["python_prefix"],
                    "worker_instance": resumed["instance"],
                    "persisted_across_front_reconnect": True, "replayed_frames": 2048,
                    "session_b_snapshot_ms": round(b_elapsed * 1000, 3),
                    "replayed_frames_when_b_recovered": replay_count_at_b,
                    "same_scope_pending_ms": round(pending_elapsed * 1000, 3),
                    "replay_and_live_sequence": "1..2049", "query_recovered_after_pending": True,
                    "total_recovery_ms": round((time.perf_counter() - start) * 1000, 3),
                    "transport": "windows_named_pipe" if os.name == "nt" else "unix_socket",
                    "runtime": "deterministic execution-state fixture; no model invocation"}
        target = os.environ.get("JIUWENSWARM_RECOVERY_EVIDENCE")
        if target:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
        await conn.send({"kind": KIND_SHUTDOWN})
        await asyncio.wait_for(reader_task, 5)
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0
    finally:
        for task in (reader_task, gateway_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (reader_task, gateway_task) if task is not None), return_exceptions=True)
        if conn is not None:
            await conn.close()
        if process.returncode is None:
            process.kill()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
