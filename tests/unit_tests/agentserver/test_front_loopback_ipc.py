# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authenticated loopback transport and attached Worker lifecycle evidence."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import psutil
import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.ipc.loopback import FrontConnectSessionSource, FrontLoopbackListener, LoopbackParameters
from jiuwenswarm.server.ipc.protocol import HEADER, dump_agent_request
from jiuwenswarm.server.ipc.session import runtime_ipc_mode
from jiuwenswarm.server.ipc.stream import StreamConnection
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor, worker_process
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker import service


def _parameters(payload: dict) -> LoopbackParameters:
    return LoopbackParameters.from_payload(payload, code_identity="a" * 64, data_scope="b" * 64)


@pytest.mark.parametrize("value,expected", [(None, "resident"), ("", "resident"),
    ("resident", "resident"), (" front_loopback ", "front_loopback")])
def test_explicit_mode_preserves_desktop_default(monkeypatch, value, expected):
    monkeypatch.delenv("JIUWEN_HARMONY_RUNTIME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_IPC", raising=False)
    if value is not None:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", value)
    assert runtime_ipc_mode() == expected


@pytest.mark.parametrize("platform,marker,path,expected", [
    ("win32", None, "", "resident"),
    ("darwin", None, "/usr/bin", "resident"),
    ("linux", None, "/usr/bin", "resident"),
    ("linux", "0", "/usr/bin", "resident"),
    ("linux", "1", "/usr/bin", "front_loopback"),
    ("ohos", None, "/system/bin", "front_loopback"),
    ("linux", None, "/data/app/el1/bundle/100/hnp/com.example/bin", "front_loopback"),
    ("linux", None, "/opt/hnp/bin", "resident"),
    ("linux", None, "/data/app/example/bin", "resident"),
])
@pytest.mark.parametrize("override", [None, "", "   "])
def test_platform_default_without_ipc_configuration(monkeypatch, platform, marker, path, expected, override):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("PATH", path)
    monkeypatch.delenv("JIUWEN_HARMONY_RUNTIME", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_IPC", raising=False)
    if marker is not None:
        monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", marker)
    if override is not None:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", override)
    assert runtime_ipc_mode() == expected


@pytest.mark.parametrize("override", ["resident", "front_loopback"])
def test_harmony_allows_explicit_transport_override(monkeypatch, override):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", override)
    assert runtime_ipc_mode() == override


def test_unknown_mode_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "typo")
    with pytest.raises(ValueError, match="Unsupported JIUWENSWARM_RUNTIME_IPC"):
        RuntimeSupervisor(Readiness())


@pytest.mark.parametrize("field,value", [
    ("host", "0.0.0.0"), ("host", "localhost"), ("port", 0), ("port", True),
    ("port", 65536), ("generation", 0), ("generation", True), ("generation", 2**63),
    ("token", "secret"), ("code_identity", "c" * 64), ("data_scope", "c" * 64),
    ("timeout", 0), ("timeout", float("nan")), ("timeout", float("inf")), ("transport", "resident"),
])
async def test_invalid_launch_parameters_rejected_without_disclosing_token(field, value):
    listener = FrontLoopbackListener()
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    payload = listener.launch_payload()
    secret = payload["token"]
    try:
        assert secret not in repr(_parameters(payload))
        payload[field] = value
        with pytest.raises(ValueError, match="stage=parameters") as error:
            _parameters(payload)
        assert secret not in str(error.value)
    finally:
        await listener.close()


@pytest.mark.parametrize("field,value", [("token", "c" * 64), ("generation", 0),
    ("data_scope", "c" * 64), ("code_identity", "c" * 64), ("kind", "hello")])
async def test_bad_auth_does_not_consume_the_legitimate_connection(field, value, caplog):
    listener = FrontLoopbackListener(timeout=3)
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    payload = listener.launch_payload()
    parameters = _parameters(payload)
    source = FrontConnectSessionSource(parameters)
    accepted = None
    outgoing = None
    reader, writer = await asyncio.open_connection("127.0.0.1", parameters.port)
    bad = StreamConnection(reader, writer)
    try:
        frame = parameters.auth_frame()
        frame[field] = value
        await bad.send(frame)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(bad.recv(), 1)
        await source.start()
        accepted = await listener.accept()
        outgoing = await source.accept()
        await listener.close()
        await outgoing.send({"kind": "hello", "generation": parameters.generation})
        assert await accepted.recv() == {"kind": "hello", "generation": parameters.generation}
        await accepted.send({"kind": "heartbeat"})
        assert await outgoing.recv() == {"kind": "heartbeat"}
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", parameters.port)
        with pytest.raises(ConnectionError, match="already consumed"):
            await source.accept()
        assert parameters.token not in caplog.text
    finally:
        await bad.close()
        if outgoing is not None:
            await outgoing.close()
        if accepted is not None:
            await accepted.close()
        await source.close()
        await listener.close()


@pytest.mark.parametrize("body", [HEADER.pack(4097), HEADER.pack(1) + b"{", HEADER.pack(0)])
async def test_malformed_auth_is_bounded_and_does_not_poison_listener(body):
    listener = FrontLoopbackListener(timeout=2)
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    parameters = _parameters(listener.launch_payload())
    reader, writer = await asyncio.open_connection("127.0.0.1", parameters.port)
    source = FrontConnectSessionSource(parameters)
    conn = worker_conn = None
    try:
        writer.write(body)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        await source.start()
        conn = await listener.accept()
        worker_conn = await source.accept()
    finally:
        writer.close()
        await writer.wait_closed()
        if conn is not None:
            await conn.close()
        if worker_conn is not None:
            await worker_conn.close()
        await source.close()
        await listener.close()


async def test_accept_timeout_reclaims_silent_connections_and_port():
    listener = FrontLoopbackListener(timeout=0.1)
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    port = listener.launch_payload()["port"]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        with pytest.raises(TimeoutError, match="stage=accept"):
            await listener.accept()
        await listener.close()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        writer.close()
        await writer.wait_closed()
        await listener.close()


async def test_worker_waits_for_transport_auth_before_runtime_load(monkeypatch):
    listener = FrontLoopbackListener(timeout=0.1)
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    payload = listener.launch_payload()
    payload["token"] = "c" * 64
    source = FrontConnectSessionSource(_parameters(payload))
    loader = AsyncMock()
    monkeypatch.setattr(service, "_load_runtime", loader)
    try:
        with pytest.raises(ConnectionError, match="stage=auth"):
            await service.serve_worker("", host="127.0.0.1", port=18092,
                                       code_identity_value="a" * 64, session_source=source)
        loader.assert_not_called()
    finally:
        await listener.close()


async def test_silent_candidate_does_not_delay_legitimate_worker():
    listener = FrontLoopbackListener(timeout=3)
    await listener.start(code_identity="a" * 64, data_scope="b" * 64)
    parameters = _parameters(listener.launch_payload())
    reader, writer = await asyncio.open_connection("127.0.0.1", parameters.port)
    source = FrontConnectSessionSource(parameters)
    conn = worker_conn = None
    try:
        await asyncio.wait_for(source.start(), 1)
        conn = await listener.accept()
        worker_conn = await source.accept()
        await listener.close()
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        if conn is not None:
            await conn.close()
        if worker_conn is not None:
            await worker_conn.close()
        await source.close()
        await listener.close()


async def test_stopping_during_auth_reclaims_worker_and_listener(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    monkeypatch.setattr(worker_process, "build_worker_command", lambda *args, **kwargs:
                        [sys.executable, "-c", "import time; time.sleep(20)"])
    launches = []
    original = FrontLoopbackListener.launch_payload

    def record(listener):
        payload = original(listener)
        launches.append(payload)
        return payload

    monkeypatch.setattr(FrontLoopbackListener, "launch_payload", record)
    supervisor = RuntimeSupervisor(Readiness(), stub=True)
    boot = asyncio.create_task(supervisor.run_until_ipc_ready())
    process = None
    try:
        await _wait_until(lambda: supervisor._process is not None and supervisor._process.pid is not None)
        process = supervisor._process
        await asyncio.wait_for(supervisor.stop(), 4)
        assert await process.wait(timeout=1) is not None
        for launch in launches:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", launch["port"])
    finally:
        boot.cancel()
        await asyncio.gather(boot, return_exceptions=True)
        await supervisor.stop()
        if process is not None:
            await process.stop()


@pytest.mark.parametrize("action", ["eof", "shutdown", "cancel"])
async def test_attached_service_drains_dispatch_before_runtime_shutdown(monkeypatch, action):
    listener = FrontLoopbackListener(timeout=3)
    from jiuwenswarm.server.supervisor.identity import data_scope

    await listener.start(code_identity="a" * 64, data_scope=data_scope())
    payload = listener.launch_payload()
    parameters = LoopbackParameters.from_payload(payload, code_identity="a" * 64, data_scope=data_scope())
    source = FrontConnectSessionSource(parameters)
    events = []
    running = asyncio.Event()

    class Runtime:
        def attach_gateway_connection(self, _ws, _lock):
            pass

        async def dispatch_parsed_request(self, ws, request, _lock):
            if request.req_method == ReqMethod.CHAT_CANCEL:
                events.append("cancel")
                self.active_task.cancel()
                return
            self.active_task = asyncio.current_task()
            events.append("dispatch")
            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                      "body": {"event_type": "chat.delta", "content": "first"}}))
            from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY

            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                      "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
                                      "body": {"event_type": "chat.final", "content": "push"}}))
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("dispatch-drained")

        async def shutdown(self):
            events.append("runtime-shutdown")

    async def load(_host, _port, _signals, dispatcher, loaded):
        runtime = Runtime()
        loaded["runtime"] = runtime
        await dispatcher.bind_runtime(runtime)
        await dispatcher.mark_ready()
        return runtime

    monkeypatch.setattr(service, "_load_runtime", load)
    task = asyncio.create_task(service.serve_worker("", host="127.0.0.1", port=18092,
                              code_identity_value="a" * 64, session_source=source))
    conn = None
    try:
        conn = await listener.accept()
        await listener.close()
        hello = await conn.recv()
        assert hello["generation"] == parameters.generation
        while (await conn.recv())["kind"] != "readiness":
            pass
        accepted = AgentRequest(request_id="chat", channel_id="web", session_id="session",
                                req_method=ReqMethod.CHAT_SEND, params={}, is_stream=True)
        await conn.send({"kind": "request", "generation": parameters.generation,
                         "dispatch_id": "dispatch", "payload": dump_agent_request(accepted)})
        await asyncio.wait_for(running.wait(), 2)
        frames = [await conn.recv(), await conn.recv()]
        assert [frame["payload"]["body"]["content"] for frame in frames] == ["first", "push"]
        assert all(frame["generation"] == parameters.generation for frame in frames)
        if action == "cancel":
            await conn.send({"kind": "cancel", "generation": parameters.generation,
                             "request_id": "cancel-chat", "session_id": "session"})
            while True:
                frame = await asyncio.wait_for(conn.recv(), 2)
                assert frame["generation"] == parameters.generation
                if frame["kind"] == "request.done" and frame["request_id"] == "cancel-chat":
                    assert frame["ok"] is True
                    break
            await _wait_until(lambda: "dispatch-drained" in events)
        if action != "eof":
            await conn.send({"kind": "shutdown"})
            while True:
                frame = await asyncio.wait_for(conn.recv(), 2)
                if frame["kind"] == "shutdown.done":
                    assert frame["ok"] is True
                    assert events[-1] == "runtime-shutdown"
                    break
        else:
            await conn.close()
        await asyncio.wait_for(task, 3)
        assert events == (["dispatch", "cancel", "dispatch-drained", "runtime-shutdown"]
                          if action == "cancel" else ["dispatch", "dispatch-drained", "runtime-shutdown"])
    finally:
        if conn is not None:
            await conn.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await listener.close()


class Capture:
    def __init__(self):
        self.frames = []

    async def send(self, payload, *, ws=None, send_lock=None):
        self.frames.append(payload)
        return True


def _forbid_resident(*args, **kwargs):
    pytest.fail("attached mode reached a resident endpoint operation")


async def _wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("expected process/state transition did not occur")
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("mode_source", ["explicit", "harmony-marker", "hnp-path"])
async def test_real_supervisor_restarts_with_fresh_credentials_and_reaps_worker(monkeypatch, tmp_path, caplog, mode_source):
    if mode_source != "explicit":
        monkeypatch.delenv("JIUWENSWARM_RUNTIME_IPC", raising=False)
        if mode_source == "harmony-marker":
            monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
        else:
            monkeypatch.delenv("JIUWEN_HARMONY_RUNTIME", raising=False)
            monkeypatch.setenv("PATH", os.pathsep.join([
                "/data/app/el1/bundle/100/hnp/com.example/bin", os.environ.get("PATH", ""),
            ]))
    else:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    for name in ("try_connect", "endpoint_is_listening", "connect_with_retry", "default_socket_path"):
        monkeypatch.setattr(runtime_supervisor, name, _forbid_resident)
    launches = []
    original = FrontLoopbackListener.launch_payload

    def record(listener):
        payload = original(listener)
        launches.append(payload)
        return payload

    monkeypatch.setattr(FrontLoopbackListener, "launch_payload", record)
    capture = Capture()
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=capture, stub=True, connect_timeout=8)
    processes = []
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 12)
        assert await readiness.wait_agent_ready(5)
        first = supervisor._process
        first_native = psutil.Process(supervisor.worker_pid)
        processes.append(first)
        assert supervisor._peer_generation == launches[0]["generation"]
        for launch in launches:
            assert launch["token"] not in " ".join(first._proc.args)
        request = AgentRequest(request_id="first", channel_id="web", session_id="one",
                               req_method=ReqMethod.CHAT_SEND, params={})
        await asyncio.wait_for(supervisor.dispatch_parsed_request(object(), request, asyncio.Lock()), 3)
        assert capture.frames[-1]["echo"] == "chat.send"
        first._proc.kill()
        await _wait_until(lambda: supervisor._process is not None and supervisor._process is not first)
        await _wait_until(lambda: supervisor._peer_generation == launches[-1]["generation"]
                          and readiness.snapshot()["agent_ready"])
        processes.append(supervisor._process)
        assert await first.wait(timeout=1) is not None
        assert not first_native.is_running()
        assert len(launches) == 2
        for field in ("token", "generation", "port"):
            assert launches[0][field] != launches[1][field]
        request.request_id = "after-restart"
        await asyncio.wait_for(supervisor.dispatch_parsed_request(object(), request, asyncio.Lock()), 3)
        assert capture.frames[-1]["request_id"] == "after-restart"
        final_native = psutil.Process(supervisor.worker_pid)
        await supervisor.stop()
        assert await processes[-1].wait(timeout=2) == 0
        assert not final_native.is_running()
        assert not list(tmp_path.rglob("*.owner.lock"))
        assert not list(tmp_path.rglob("*.startup.lock"))
        assert not list(tmp_path.rglob("*.sock"))
        for launch in launches:
            assert launch["token"] not in caplog.text
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", launch["port"])
    finally:
        await supervisor.stop()
        for process in processes:
            await process.stop()


@pytest.mark.parametrize("failure", ["spawn", "exit-before-auth", "hello", "hello-generation", "gateway-attach"])
async def test_failed_startup_reclaims_listener_stream_and_process(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    launches = []
    processes = []
    original_payload = FrontLoopbackListener.launch_payload
    original_spawn = worker_process.WorkerProcess.spawn

    def record(listener):
        payload = original_payload(listener)
        launches.append(payload)
        return payload

    async def spawn(process):
        processes.append(process)
        await original_spawn(process)

    monkeypatch.setattr(FrontLoopbackListener, "launch_payload", record)
    monkeypatch.setattr(worker_process.WorkerProcess, "spawn", spawn)
    if failure == "spawn":
        def fail_popen(*args, **kwargs):
            raise OSError("injected spawn failure")
        monkeypatch.setattr(worker_process.subprocess, "Popen", fail_popen)
    elif failure == "exit-before-auth":
        monkeypatch.setattr(worker_process, "build_worker_command", lambda *args, **kwargs:
                            [sys.executable, "-c", "import sys; sys.exit(17)"])
    elif failure == "hello":
        monkeypatch.setattr(runtime_supervisor, "PROTOCOL_VERSION", -1)
    elif failure == "hello-generation":
        original_connect = RuntimeSupervisor._connect_attached

        async def change_generation(supervisor):
            identity = await original_connect(supervisor)
            supervisor._launch_generation += 1
            return identity

        monkeypatch.setattr(RuntimeSupervisor, "_connect_attached", change_generation)
    if failure == "gateway-attach":
        original_start = RuntimeSupervisor._start_side_tasks

        def fail_gateway_send(supervisor):
            original_start(supervisor)
            supervisor._conn.send = AsyncMock(side_effect=ConnectionError("injected gateway attach failure"))

        monkeypatch.setattr(RuntimeSupervisor, "_start_side_tasks", fail_gateway_send)
    supervisor = RuntimeSupervisor(Readiness(), stub=True, connect_timeout=3, max_restarts=0)
    if failure == "gateway-attach":
        supervisor._gateway_ws = object()
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 8)
        assert launches
        for process in processes:
            assert await process.wait(timeout=1) is not None
        for launch in launches:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", launch["port"])
        assert supervisor._conn is None
        assert supervisor._process is None
    finally:
        await supervisor.stop()


async def test_killing_real_front_exits_attached_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    script = Path(__file__).with_name("fixtures") / "loopback_front.py"
    output = tmp_path / "front.json"
    front = subprocess.Popen([sys.executable, str(script), str(output)], env=os.environ.copy(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = actual_front = None
    try:
        await _wait_until(output.exists, timeout=15)
        state = json.loads(output.read_text())
        worker = psutil.Process(state["worker_pid"])
        actual_front = psutil.Process(state["front_pid"])
        actual_front.kill()
        await asyncio.to_thread(actual_front.wait, 5)
        await asyncio.to_thread(worker.wait, 8)
        assert not worker.is_running()
        assert not list(tmp_path.rglob("*.sock"))
    finally:
        for process in (worker, actual_front):
            if process is not None and process.is_running():
                process.kill()
                await asyncio.to_thread(process.wait, 5)
        if front.poll() is None:
            front.kill()
        await asyncio.to_thread(front.wait, 5)


async def test_default_native_transport_survives_front_restart(monkeypatch, tmp_path):
    """An ordinary desktop still uses its original resident transport."""
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.ipc.stream import PipeConnection
    import uuid

    monkeypatch.delenv("JIUWENSWARM_RUNTIME_IPC", raising=False)
    monkeypatch.delenv("JIUWEN_HARMONY_RUNTIME", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                                             if not ("/data/app/" in entry and "/hnp/" in entry)))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    monkeypatch.setattr(runtime_supervisor, "FrontLoopbackListener", _forbid_resident)
    assert runtime_ipc_mode() == "resident"
    path = str(tmp_path / "resident.pipe") if os.name == "nt" else f"/tmp/jw-native-{uuid.uuid4().hex}.sock"
    first = RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
    second = None
    process = native = conn = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 15)
        assert await first._readiness.wait_agent_ready(5)
        process = first._process
        native = psutil.Process(first.worker_pid)
        assert isinstance(first._conn, PipeConnection if os.name == "nt" else StreamConnection)
        await first.stop()
        assert process.returncode is None and native.is_running()
        second = RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
        await asyncio.wait_for(second.run_until_ipc_ready(), 15)
        assert second.worker_pid == native.pid
        assert not second.owns_process
        await second.stop()
        conn = await connect_with_retry(path, timeout=5)
        assert (await asyncio.wait_for(conn.recv(), 5))["kind"] == "hello"
        assert (await asyncio.wait_for(conn.recv(), 5))["kind"] == "readiness"
        await conn.send({"kind": "shutdown"})
        try:
            reply = await asyncio.wait_for(conn.recv(), 5)
            assert reply == {"kind": "shutdown.done", "ok": True}
        except ConnectionError:
            # IPC v1 has no resident shutdown acknowledgement.
            from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
            assert PROTOCOL_VERSION == 1
        assert await process.wait(timeout=5) == 0
        assert not native.is_running()
    finally:
        if conn is not None:
            await conn.close()
        if second is not None:
            await second.stop()
        await first.stop()
        if process is not None:
            await process.stop()
        if native is not None and native.is_running():
            native.kill()
            await asyncio.to_thread(native.wait, 5)


async def test_failed_startup_cleanup_reports_failure_without_hanging(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    monkeypatch.setattr(worker_process.WorkerProcess, "spawn", AsyncMock(side_effect=OSError("injected spawn failure")))
    supervisor = RuntimeSupervisor(Readiness(), stub=True)
    monkeypatch.setattr(supervisor, "_retire_unhealthy_worker", AsyncMock(side_effect=RuntimeError("PID identity changed")))
    try:
        with pytest.raises(RuntimeError, match="startup cleanup failed"):
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 2)
        assert supervisor._conn is None and supervisor._process is None
        assert supervisor._readiness.state.value == "FAILED"
    finally:
        await supervisor.stop()
