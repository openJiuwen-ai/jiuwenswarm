# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 2: Runtime Worker IPC, supervisor routing, stream, and cancel."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, PermissionContext
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.keys import AgentRef
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.server.ipc.client import connect, endpoint_is_listening
from jiuwenswarm.server.ipc.protocol import (
    KIND_CANCEL,
    KIND_GATEWAY_ATTACH,
    KIND_GATEWAY_DETACH,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    KIND_WIRE,
    decode_frame,
    dump_agent_request,
    encode_frame,
    load_agent_request,
)
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor.affinity import SessionAffinity
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.service import (
    WorkerForceExit,
    _close_worker,
    _read_session,
    _retire_session,
    _runtime_for_shutdown,
)
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


def _socket_path() -> str:
    return f"/tmp/jwcw-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"


def _request(method: ReqMethod, *, is_stream: bool = False) -> AgentRequest:
    return AgentRequest(
        request_id=uuid.uuid4().hex,
        channel_id="web",
        session_id="sess-1",
        req_method=method,
        params={"query": "hello"},
        is_stream=is_stream,
        permission_context=PermissionContext(principal_user_id="owner", channel_id="web"),
        agent_ref=AgentRef(mode="agent", id="default"),
        user_id="user-1",
    )


class _Capture:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, payload: dict, *, ws: object = None, send_lock: object = None) -> bool:
        _ = ws, send_lock
        self.frames.append(payload)
        return True


class _FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.attached = False
        self.detached: object = None
        self.disconnects: list[object] = []

    def attach_gateway_connection(self, ws: object, send_lock: asyncio.Lock) -> None:
        _ = ws, send_lock
        self.attached = True

    async def on_gateway_disconnect(self, ws: object, remote: object) -> None:
        _ = ws
        self.detached = remote
        self.disconnects.append(remote)

    async def dispatch_parsed_request(
        self, ws: object, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        _ = send_lock
        self.requests.append(request)
        await ws.send(json.dumps({"request_id": request.request_id, "ok": True, "method": request.req_method.value if request.req_method else ""}))


def test_frame_and_request_roundtrip() -> None:
    request = _request(ReqMethod.CHAT_SEND, is_stream=True)
    encoded = encode_frame({"kind": KIND_REQUEST, "payload": dump_agent_request(request)})
    decoded = decode_frame(encoded[4:])
    restored = load_agent_request(decoded["payload"])
    assert restored.request_id == request.request_id
    assert restored.req_method is ReqMethod.CHAT_SEND
    assert restored.is_stream is True
    assert restored.permission_context is not None
    assert restored.permission_context.principal_user_id == "owner"
    assert restored.agent_ref == AgentRef(mode="agent", id="default")
    assert restored.user_id == "user-1"


def test_session_affinity_pins_primary_worker() -> None:
    affinity = SessionAffinity()
    assert affinity.worker_for("alpha") == "primary"
    assert affinity.worker_for("alpha") == "primary"
    assert affinity.worker_for("") == "primary"
    affinity.forget("alpha")
    assert affinity.worker_for("beta") == "primary"


def test_rewarm_blocks_until_next_agent_ready() -> None:
    readiness = Readiness()
    readiness.mark_transport_ready()
    readiness.mark_control_ready()
    readiness.mark_runtime_warming()
    readiness.mark_agent_ready()
    readiness.mark_runtime_warming()
    assert readiness.state is ReadinessState.RUNTIME_WARMING
    assert readiness.snapshot()["agent_ready"] is False


@pytest.mark.asyncio
async def test_rewarm_wait_does_not_return_ready() -> None:
    readiness = Readiness()
    readiness.mark_agent_ready()
    readiness.mark_runtime_warming()
    assert await readiness.wait_agent_ready(0.05) is False
    readiness.mark_agent_ready()
    assert await readiness.wait_agent_ready(0.05) is True


@pytest.mark.asyncio
async def test_ipc_request_stream_and_cancel() -> None:
    path = _socket_path()
    server = IpcServer(path)
    await server.start()
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    runtime = _FakeRuntime()

    async def _serve() -> None:
        conn = await server.accept()
        holder.open_session(conn)
        await dispatcher.bind_runtime(runtime)
        while True:
            message = await conn.recv()
            if not await dispatcher.handle(message, conn):
                break

    serve_task = asyncio.create_task(_serve())
    try:
        client = await connect(path)
        request = _request(ReqMethod.CHAT_SEND, is_stream=True)
        await client.send({"kind": KIND_REQUEST, "payload": dump_agent_request(request)})
        wire = await asyncio.wait_for(client.recv(), 2)
        done = await asyncio.wait_for(client.recv(), 2)
        assert wire["kind"] == KIND_WIRE
        assert wire["generation"] == 1
        assert wire["payload"]["method"] == "chat.send"
        assert done["kind"] == KIND_REQUEST_DONE
        assert done["generation"] == wire["generation"]
        assert done["ok"] is True
        assert done["delivered"] is True

        await client.send(
            {
                "kind": KIND_CANCEL,
                "request_id": "cancel-1",
                "session_id": "sess-1",
                "channel_id": "web",
            }
        )
        cancel_wire = await asyncio.wait_for(client.recv(), 2)
        cancel_done = await asyncio.wait_for(client.recv(), 2)
        assert cancel_wire["payload"]["method"] == ReqMethod.CHAT_CANCEL.value
        assert cancel_done["ok"] is True
        assert runtime.requests[-1].req_method is ReqMethod.CHAT_CANCEL
        await client.send({"kind": "shutdown"})
        await asyncio.wait_for(serve_task, 2)
    finally:
        if not serve_task.done():
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)
        await server.close()


@pytest.mark.asyncio
async def test_supervisor_stub_dispatches_stream_and_cancel() -> None:
    path = _socket_path()
    capture = _Capture()
    readiness = Readiness()
    readiness.mark_runtime_warming()
    supervisor = RuntimeSupervisor(
        readiness,
        forwarder=capture,  # type: ignore[arg-type]
        socket_path=path,
        stub=True,
        stub_ready_delay=0.2,
        max_restarts=0,
    )
    admission = ExecutionAdmission(readiness, forwarder=capture, wait_seconds=5)  # type: ignore[arg-type]
    admission.attach_backend(supervisor)
    boot = asyncio.create_task(supervisor.run_until_ipc_ready())
    chat = _request(ReqMethod.CHAT_SEND, is_stream=True)
    queued = asyncio.create_task(admission.dispatch(object(), chat, asyncio.Lock()))
    try:
        await asyncio.wait_for(boot, 5)
        await asyncio.wait_for(queued, 5)
        assert readiness.state is ReadinessState.AGENT_READY
        assert supervisor.worker_for_session(chat.session_id) == "primary"
        assert supervisor.registry.get("primary") is not None
        assert supervisor.registry.get("primary").ready is True
        joined = json.dumps(capture.frames)
        assert "runtime.warming" in joined
        assert "chat.send" in joined

        capture.frames.clear()
        cancel = _request(ReqMethod.CHAT_CANCEL)
        cancel.session_id = chat.session_id
        await supervisor.dispatch_parsed_request(object(), cancel, asyncio.Lock())
        assert "chat.interrupt" in json.dumps(capture.frames)
    finally:
        await supervisor.stop(terminate_worker=True)


@pytest.mark.asyncio
async def test_worker_crash_fails_inflight_request() -> None:
    path = _socket_path()
    capture = _Capture()
    readiness = Readiness()
    readiness.mark_runtime_warming()
    supervisor = RuntimeSupervisor(
        readiness,
        forwarder=capture,  # type: ignore[arg-type]
        socket_path=path,
        stub=True,
        stub_exit_on_request=True,
        max_restarts=0,
    )
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 5)
        await asyncio.wait_for(readiness.wait_agent_ready(2), 3)
        request = _request(ReqMethod.CHAT_RESUME)
        await asyncio.wait_for(
            supervisor.dispatch_parsed_request(object(), request, asyncio.Lock()),
            5,
        )
        joined = json.dumps(capture.frames)
        assert "WORKER_CLOSED" in joined
        for _ in range(50):
            if readiness.state is ReadinessState.FAILED:
                break
            await asyncio.sleep(0.02)
        assert readiness.state is ReadinessState.FAILED
    finally:
        await supervisor.stop(terminate_worker=True)


class _QueueConn:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)

    async def recv(self) -> dict:
        if not self._messages:
            raise ConnectionError("ipc closed")
        return self._messages.pop(0)

    async def close(self) -> None:
        return None


class _FailOnceForwarder:
    def __init__(self) -> None:
        self.calls = 0
        self.frames: list[dict] = []

    async def send(self, payload: dict, *, ws: object = None, send_lock: object = None) -> bool:
        _ = ws, send_lock
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("gateway websocket closed")
        self.frames.append(payload)
        return True


@pytest.mark.asyncio
async def test_forward_failure_does_not_end_worker_session() -> None:
    forwarder = _FailOnceForwarder()
    supervisor = RuntimeSupervisor(
        Readiness(),
        forwarder=forwarder,  # type: ignore[arg-type]
        socket_path=_socket_path(),
        max_restarts=0,
    )
    supervisor._peer_generation = 1
    supervisor._conn = _QueueConn(  # type: ignore[assignment]
        [
            {"kind": KIND_WIRE, "generation": 1, "request_id": "r1", "payload": {"event": "first"}},
            {"kind": KIND_WIRE, "generation": 1, "request_id": "r2", "payload": {"event": "second"}},
        ]
    )
    supervisor._routes["r1"] = (object(), asyncio.Lock())
    supervisor._routes["r2"] = (object(), asyncio.Lock())
    supervisor._gateway_ws = object()
    supervisor._gateway_lock = asyncio.Lock()
    with pytest.raises(ConnectionError, match="ipc closed"):
        await supervisor._pump()
    assert forwarder.calls == 2
    assert forwarder.frames == [{"event": "second"}]
    assert supervisor._restart_count == 0


def _server_push(request_id: str) -> dict:
    return {
        "kind": KIND_WIRE,
        "generation": 1,
        "request_id": request_id,
        "payload": {
            "request_id": request_id,
            "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
        },
    }


@pytest.mark.asyncio
async def test_unrouted_request_wire_does_not_reach_current_gateway() -> None:
    forwarder = _Capture()
    supervisor = RuntimeSupervisor(
        Readiness(),
        forwarder=forwarder,  # type: ignore[arg-type]
        socket_path=_socket_path(),
        max_restarts=0,
    )
    supervisor._peer_generation = 1
    supervisor._gateway_ws = object()
    supervisor._gateway_lock = asyncio.Lock()
    supervisor._conn = _QueueConn(  # type: ignore[assignment]
        [
            {"kind": KIND_WIRE, "generation": 1, "request_id": "old", "payload": {"event": "stale"}},
            {"kind": KIND_WIRE, "generation": 2, "request_id": "old", "payload": {"event": "other-generation"}},
            _server_push("zen-models-ready"),
        ]
    )
    with pytest.raises(ConnectionError, match="ipc closed"):
        await supervisor._pump()
    assert forwarder.frames == [
        {"request_id": "zen-models-ready", "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True}}
    ]


class _RecordingConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        return None


class _BlockingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    def attach_gateway_connection(self, ws: object, send_lock: asyncio.Lock) -> None:
        _ = ws, send_lock

    async def dispatch_parsed_request(
        self, ws: object, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        _ = send_lock
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        send = getattr(ws, "send")
        await send(json.dumps({"request_id": request.request_id, "event": "late"}))


@pytest.mark.asyncio
async def test_reconnect_drops_old_generation_output() -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    runtime = _BlockingRuntime()
    first = _RecordingConn()
    second = _RecordingConn()
    holder.open_session(first)
    await dispatcher.bind_runtime(runtime)
    request = _request(ReqMethod.CHAT_SEND, is_stream=True)
    dispatcher._spawn(dispatcher._run_request(request, holder.generation), holder.generation)
    await asyncio.wait_for(runtime.started.wait(), 1)
    old = holder.close_session()
    await dispatcher.retire_generation(old)
    holder.open_session(second)
    runtime.release.set()
    assert runtime.cancelled is True
    assert second.sent == []
    assert first.sent == []


@pytest.mark.asyncio
async def test_stale_generation_output_stays_off_the_new_connection() -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    runtime = _BlockingRuntime()
    first = _RecordingConn()
    second = _RecordingConn()
    holder.open_session(first)
    await dispatcher.bind_runtime(runtime)
    request = _request(ReqMethod.CHAT_SEND, is_stream=True)
    task = asyncio.create_task(dispatcher._run_request(request, 1))
    await asyncio.wait_for(runtime.started.wait(), 1)
    holder.open_session(second)
    runtime.release.set()
    await task
    assert runtime.cancelled is False
    assert first.sent == []
    assert second.sent == []


@pytest.mark.asyncio
async def test_shutdown_stops_dispatch_before_runtime_shutdown() -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    order: list[str] = []
    task: asyncio.Task[None] | None = None

    class _Runtime:
        async def dispatch_parsed_request(
            self, ws: object, request: AgentRequest, send_lock: asyncio.Lock
        ) -> None:
            _ = ws, request, send_lock
            order.append("dispatch")
            await asyncio.Event().wait()

        async def shutdown(self) -> None:
            assert task is not None and task.done()
            order.append("runtime-shutdown")

    class _Server:
        async def close(self) -> None:
            order.append("ipc-close")

    runtime = _Runtime()
    holder.open_session(_RecordingConn())
    await dispatcher.bind_runtime(runtime)
    dispatcher._spawn(dispatcher._run_request(_request(ReqMethod.CHAT_SEND), 1), 1)
    task = next(iter(dispatcher._tasks))
    for _ in range(50):
        if order:
            break
        await asyncio.sleep(0.01)
    assert order == ["dispatch"]
    await _close_worker(dispatcher, None, {"runtime": runtime}, _Server(), dispatch_timeout=1)
    assert order == ["dispatch", "runtime-shutdown", "ipc-close"]


@pytest.mark.asyncio
async def test_drain_timeout_skips_runtime_shutdown() -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    order: list[str] = []
    release = asyncio.Event()

    class _Runtime:
        async def dispatch_parsed_request(
            self, ws: object, request: AgentRequest, send_lock: asyncio.Lock
        ) -> None:
            _ = ws, request, send_lock
            order.append("dispatch")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("swallowed-cancel")
                await release.wait()

        async def shutdown(self) -> None:
            order.append("runtime-shutdown")

    class _Server:
        async def close(self) -> None:
            order.append("ipc-close")

    runtime = _Runtime()
    holder.open_session(_RecordingConn())
    await dispatcher.bind_runtime(runtime)
    dispatcher._spawn(dispatcher._run_request(_request(ReqMethod.CHAT_SEND), 1), 1)
    task = next(iter(dispatcher._tasks))
    for _ in range(50):
        if order:
            break
        await asyncio.sleep(0.01)
    try:
        with pytest.raises(WorkerForceExit):
            await _close_worker(
                dispatcher,
                None,
                {"runtime": runtime},
                _Server(),
                dispatch_timeout=0.2,
            )
        assert order == ["dispatch", "swallowed-cancel"]
        assert task.done() is False
    finally:
        release.set()
        await asyncio.wait({task}, timeout=1)


@pytest.mark.asyncio
async def test_worker_queue_rejects_when_full_and_cancel_drops_queued() -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    conn = _RecordingConn()
    holder.open_session(conn)
    from jiuwenswarm.server.worker.dispatcher import _MAX_QUEUED_REQUESTS

    for index in range(_MAX_QUEUED_REQUESTS):
        request = _request(ReqMethod.CHAT_SEND)
        request.request_id = f"q{index}"
        await dispatcher._run_request(request, holder.generation)
    assert len(dispatcher._queued) == _MAX_QUEUED_REQUESTS
    overflow = _request(ReqMethod.CHAT_SEND)
    overflow.request_id = "overflow"
    await dispatcher._run_request(overflow, holder.generation)
    assert len(dispatcher._queued) == _MAX_QUEUED_REQUESTS
    assert conn.sent[-1]["kind"] == KIND_REQUEST_DONE
    assert conn.sent[-1]["request_id"] == "overflow"
    assert conn.sent[-1]["ok"] is False
    cancel = _request(ReqMethod.CHAT_CANCEL)
    cancel.request_id = "cancel-q0"
    cancel.session_id = None
    cancel.params = {"request_id": "q0"}
    assert await dispatcher._drop_queued_cancel(cancel) is True
    assert all(item[1].request_id != "q0" for item in dispatcher._queued)


class _ScriptedConn:
    def __init__(self, message: object | None = None, *, hang: bool = False) -> None:
        self._message = message
        self._hang = hang
        self.closed = False

    async def recv(self) -> object:
        if self._hang:
            await asyncio.Event().wait()
        return self._message

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "match"),
    [
        ({"kind": "heartbeat", "protocol": 1}, "expected hello"),
        ({"kind": "hello", "protocol": 99}, "protocol mismatch"),
    ],
)
async def test_bring_up_closes_conn_when_hello_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    message: dict,
    match: str,
) -> None:
    conn = _ScriptedConn(message)

    async def _try_connect(path: str, timeout: float = 0.5) -> _ScriptedConn:
        _ = path, timeout
        return conn

    monkeypatch.setattr(
        "jiuwenswarm.server.supervisor.runtime_supervisor.try_connect",
        _try_connect,
    )
    supervisor = RuntimeSupervisor(Readiness(), socket_path=_socket_path(), max_restarts=0)
    with pytest.raises(RuntimeError, match=match):
        await supervisor._bring_up()
    assert conn.closed is True
    assert supervisor._conn is None
    assert supervisor.owns_process is False


@pytest.mark.asyncio
async def test_bring_up_closes_conn_when_hello_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ScriptedConn(hang=True)

    async def _try_connect(path: str, timeout: float = 0.5) -> _ScriptedConn:
        _ = path, timeout
        return conn

    monkeypatch.setattr(
        "jiuwenswarm.server.supervisor.runtime_supervisor.try_connect",
        _try_connect,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.supervisor.runtime_supervisor._HELLO_TIMEOUT",
        0.05,
    )
    supervisor = RuntimeSupervisor(Readiness(), socket_path=_socket_path(), max_restarts=0)
    with pytest.raises(asyncio.TimeoutError):
        await supervisor._bring_up()
    assert conn.closed is True
    assert supervisor._conn is None


async def _serve_one_session(server: IpcServer, holder: ConnHolder, dispatcher: WorkerDispatcher) -> None:
    conn = await server.accept()
    holder.open_session(conn)
    try:
        await _read_session(conn, dispatcher)
    except (OSError, asyncio.IncompleteReadError):
        await _retire_session(holder, dispatcher)


@pytest.mark.asyncio
async def test_ipc_drop_runs_gateway_disconnect_once() -> None:
    path = _socket_path()
    server = IpcServer(path)
    await server.start()
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    runtime = _FakeRuntime()
    await dispatcher.bind_runtime(runtime)
    serve = asyncio.create_task(_serve_one_session(server, holder, dispatcher))
    try:
        client = await connect(path)
        await client.send({"kind": KIND_GATEWAY_ATTACH})
        for _ in range(50):
            if runtime.attached:
                break
            await asyncio.sleep(0.01)
        assert runtime.attached is True
        await client.close()
        await asyncio.wait_for(serve, 2)
        assert runtime.disconnects == ["supervisor disconnected"]
    finally:
        if not serve.done():
            serve.cancel()
            await asyncio.gather(serve, return_exceptions=True)
        await server.close()


@pytest.mark.asyncio
async def test_explicit_gateway_detach_is_not_repeated_on_ipc_drop() -> None:
    path = _socket_path()
    server = IpcServer(path)
    await server.start()
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    runtime = _FakeRuntime()
    await dispatcher.bind_runtime(runtime)
    serve = asyncio.create_task(_serve_one_session(server, holder, dispatcher))
    try:
        client = await connect(path)
        await client.send({"kind": KIND_GATEWAY_ATTACH})
        for _ in range(50):
            if runtime.attached:
                break
            await asyncio.sleep(0.01)
        await client.send({"kind": KIND_GATEWAY_DETACH, "remote": "front-stop"})
        for _ in range(50):
            if runtime.disconnects:
                break
            await asyncio.sleep(0.01)
        await client.close()
        await asyncio.wait_for(serve, 2)
        assert runtime.disconnects == ["front-stop"]
    finally:
        if not serve.done():
            serve.cancel()
            await asyncio.gather(serve, return_exceptions=True)
        await server.close()


async def _shutdown_resident_worker(path: str) -> None:
    """Ask a listening Worker to exit, then drop the IPC connection."""
    conn = await connect(path)
    try:
        await asyncio.wait_for(conn.recv(), 2)
        await conn.send({"kind": KIND_SHUTDOWN})
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_front_stop_leaves_spawned_worker_running() -> None:
    path = _socket_path()
    first_ready = Readiness()
    first_ready.mark_runtime_warming()
    first = RuntimeSupervisor(
        first_ready,
        socket_path=path,
        stub=True,
        max_restarts=0,
    )
    second: RuntimeSupervisor | None = None
    owned = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 5)
        assert await first_ready.wait_agent_ready(2) is True
        assert first.owns_process is True
        assert first.runtime_loaded is False
        instance = first.worker_instance
        assert instance
        owned = first._process
        await first.stop()
        assert owned is not None and owned.returncode is None
        second_ready = Readiness()
        second_ready.mark_runtime_warming()
        second = RuntimeSupervisor(
            second_ready,
            socket_path=path,
            stub=True,
            max_restarts=0,
        )
        await asyncio.wait_for(second.run_until_ipc_ready(), 5)
        assert second.owns_process is False
        assert second.worker_instance == instance
        assert second.runtime_loaded is True
        assert await second_ready.wait_agent_ready(2) is True
        await second.stop(terminate_worker=True)
        assert endpoint_is_listening(path)
        assert owned.returncode is None
    finally:
        if second is not None and not second._stopping:
            await second.stop()
        if endpoint_is_listening(path):
            try:
                await _shutdown_resident_worker(path)
            except (OSError, asyncio.TimeoutError):
                pass
        if owned is not None and owned.returncode is None:
            await owned.wait(timeout=2)
            if owned.returncode is None:
                await owned.stop()


@pytest.mark.asyncio
async def test_unix_socket_is_owner_only() -> None:
    if os.name == "nt":
        pytest.skip("unix socket mode applies to POSIX")
    path = _socket_path()
    server = IpcServer(path)
    await server.start()
    try:
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600
        client = await connect(path)
        hello_task = asyncio.create_task(server.accept())
        conn = await asyncio.wait_for(hello_task, 2)
        await conn.close()
        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_supervisor_reattaches_without_spawning() -> None:
    path = _socket_path()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        path,
        "--stub",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    readiness = Readiness()
    readiness.mark_runtime_warming()
    supervisor = RuntimeSupervisor(
        readiness,
        socket_path=path,
        stub=True,
        max_restarts=0,
    )
    try:
        for _ in range(50):
            if endpoint_is_listening(path):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("stub worker did not bind a socket")
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 5)
        assert supervisor.owns_process is False
        assert supervisor.worker_instance
        assert await readiness.wait_agent_ready(2) is True
        assert proc.returncode is None
    finally:
        await supervisor.stop()
        if proc.returncode is None and endpoint_is_listening(path):
            try:
                await _shutdown_resident_worker(path)
                await asyncio.wait_for(proc.wait(), 2)
            except (OSError, asyncio.TimeoutError):
                proc.terminate()
                await proc.wait()
        elif proc.returncode is None:
            proc.terminate()
            await proc.wait()


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_out_a_stuck_runtime_load() -> None:
    release = threading.Event()
    entered = threading.Event()

    async def _load() -> object:
        await asyncio.to_thread(_block_until_released, entered, release)
        return {"runtime": "late"}

    task = asyncio.create_task(_load())
    assert await asyncio.to_thread(entered.wait, 1.0)
    started = time.monotonic()
    try:
        runtime = await _runtime_for_shutdown(task, {"runtime": None}, timeout=0.2)
        assert runtime is None
        assert time.monotonic() - started < 1.0
        assert task.cancelling()
    finally:
        release.set()
        await asyncio.wait({task}, timeout=1.0)


@pytest.mark.asyncio
async def test_shutdown_uses_runtime_that_finishes_loading() -> None:
    async def _load() -> str:
        return "ready"

    task = asyncio.create_task(_load())
    assert await _runtime_for_shutdown(task, {"runtime": None}, timeout=1.0) == "ready"


def _block_until_released(entered: threading.Event, release: threading.Event) -> None:
    entered.set()
    release.wait(timeout=2.0)


def test_front_and_supervisor_import_do_not_load_runtime() -> None:
    script = r"""
import os
import sys
os.environ["JIUWENSWARM_RUNTIME_WORKSPACE_READY"] = "1"
forbidden = (
    "openjiuwen",
    "jiuwenswarm.server.agent_ws_server",
    "jiuwenswarm.server.worker.lifecycle",
    "jiuwenswarm.agents.harness",
)
import jiuwenswarm.server.ipc.protocol  # noqa: F401
import jiuwenswarm.server.supervisor.runtime_supervisor  # noqa: F401
import jiuwenswarm.server.worker.main  # noqa: F401
import jiuwenswarm.server.worker.service  # noqa: F401
import jiuwenswarm.server.app_agentserver  # noqa: F401
hits = [
    name for name in sys.modules
    if name in forbidden or any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
]
if hits:
    raise SystemExit("unexpected modules: " + ",".join(sorted(hits)[:20]))
"""
    result = __import__("subprocess").run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_worker_lifecycle_owns_runtime_construct() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "jiuwenswarm/server/worker/lifecycle.py").read_text(encoding="utf-8")
    assert "bind_transport=False" in text
    assert "AgentWebSocketServer.get_instance" in text
