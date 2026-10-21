# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise IPC under real OS backpressure and executor saturation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.ipc.client import connect
from jiuwenswarm.server.ipc.protocol import KIND_HEARTBEAT
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module
from jiuwenswarm.server.supervisor import worker_process as process_module
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@asynccontextmanager
async def _pair():
    server = IpcServer(f"/tmp/jw-pressure-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
    await server.start()
    client = peer = None
    try:
        client = await connect(server.path)
        peer = await asyncio.wait_for(server.accept(), 2)
        yield client, peer, server
    finally:
        if client is not None:
            await client.close()
        if peer is not None:
            await peer.close()
        await server.close()


@asynccontextmanager
async def _saturated_default_pool():
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-saturated")
    loop.set_default_executor(pool)
    release = threading.Event()
    started = [threading.Event(), threading.Event()]

    def block(event):
        event.set()
        release.wait(120)

    blockers = [loop.run_in_executor(None, block, event) for event in started]
    try:
        async with asyncio.timeout(2):
            while not all(event.is_set() for event in started):
                await asyncio.sleep(0.005)
        yield blockers
    finally:
        release.set()
        await asyncio.gather(*blockers)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe executor isolation")
async def test_named_pipe_heartbeat_survives_default_pool_saturation() -> None:
    async with _pair() as (client, peer, _server):
        async with _saturated_default_pool():

            async def exchange():
                await client.send({"kind": KIND_HEARTBEAT})
                assert (await peer.recv())["kind"] == KIND_HEARTBEAT
                await peer.send({"kind": KIND_HEARTBEAT})
                return await client.recv()

            assert (await asyncio.wait_for(exchange(), 0.5))["kind"] == KIND_HEARTBEAT


@pytest.mark.asyncio
async def test_blocked_business_write_does_not_block_receiving_heartbeat() -> None:
    async with _pair() as (client, peer, _server):
        blocked = asyncio.create_task(client.send({"payload": "x" * (4 * 1024 * 1024)}))
        try:
            await asyncio.sleep(0.05)
            assert not blocked.done(), (
                "OS write should be blocked until its peer drains"
            )
            await peer.send({"kind": KIND_HEARTBEAT})
            assert (await asyncio.wait_for(client.recv(), 1))["kind"] == KIND_HEARTBEAT
            assert not blocked.done()
            frame = await asyncio.wait_for(peer.recv(), 3)
            assert len(frame["payload"]) == 4 * 1024 * 1024
            await asyncio.wait_for(blocked, 3)
        finally:
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe executor isolation")
async def test_connect_and_close_work_while_default_pool_is_saturated() -> None:
    async with _saturated_default_pool():

        async def connect_and_close():
            async with _pair() as (client, peer, _server):
                waiting = asyncio.create_task(client.recv())
                await asyncio.sleep(0.03)
                await client.close()
                with pytest.raises(ConnectionError):
                    await waiting
                await peer.close()

        await asyncio.wait_for(connect_and_close(), 1)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows native write cancellation")
async def test_cancelled_native_write_closes_pipe_before_reuse() -> None:
    async with _pair() as (client, peer, _server):
        write = asyncio.create_task(client.send({"payload": "x" * (4 * 1024 * 1024)}))
        await asyncio.sleep(0.05)
        assert not write.done()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(write, 1)
        with pytest.raises(ConnectionError):
            await client.send({"kind": KIND_HEARTBEAT})
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(peer.recv(), 1)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows pipe-owned executor cleanup")
async def test_peer_disconnect_releases_pipe_io_threads_without_gc() -> None:
    async with _pair() as (client, peer, _server):
        read = asyncio.create_task(client.recv())
        await asyncio.sleep(0.03)
        await peer.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(read, 1)
        assert client._closed
        # Keep client referenced: cleanup must not depend on garbage collection.
        threads = list(client._reader_pool._threads) + list(
            client._writer_pool._threads
        )
        async with asyncio.timeout(1):
            while any(thread.is_alive() for thread in threads):
                await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_competing_business_writes_preserve_frames_and_heartbeats() -> None:
    async with _pair() as (client, peer, _server):

        async def receive():
            frames = []
            for _ in range(20):
                frame = await peer.recv()
                frames.append(frame["sequence"])
                assert len(frame["payload"]) == 256 * 1024
                await peer.send({"kind": KIND_HEARTBEAT})
            return frames

        async def receive_heartbeats():
            for _ in range(20):
                assert (await client.recv())["kind"] == KIND_HEARTBEAT

        async with asyncio.timeout(5):
            receiver = asyncio.create_task(receive())
            heartbeats = asyncio.create_task(receive_heartbeats())
            await asyncio.gather(
                *[
                    client.send({"sequence": sequence, "payload": "x" * (256 * 1024)})
                    for sequence in range(20)
                ]
            )
            assert await receiver == list(range(20))
            await heartbeats
        assert client.diagnostics()["last_tx_age"] is not None
        assert client.diagnostics()["last_rx_age"] is not None


@pytest.fixture(autouse=True)
def _capture_logs(caplog):
    loggers = [supervisor_module.logger, process_module.logger]
    for logger in loggers:
        logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeHandler(caplog.handler)


@asynccontextmanager
async def _worker(mode, tmp_path, monkeypatch, *, max_restarts=0):
    fixture = Path(__file__).parent / "fixtures" / "diagnostic_worker.py"
    root = Path(__file__).resolve().parents[3]
    python_paths = [str(root)]
    if os.environ.get("PYTHONPATH"):
        python_paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(python_paths))

    def command(socket_path, **options):
        return [sys.executable, str(fixture), "--mode", mode, "--socket", socket_path]

    monkeypatch.setattr(process_module, "build_worker_command", command)
    endpoint = (
        str(tmp_path / "worker.pipe")
        if os.name == "nt"
        else f"/tmp/jw-diag-{uuid.uuid4().hex[:8]}.sock"
    )
    supervisor = RuntimeSupervisor(
        Readiness(),
        forwarder=EventForwarder(),
        socket_path=endpoint,
        max_restarts=max_restarts,
    )
    process = None
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 10)
        process = supervisor._process
        assert process is not None and supervisor.worker_pid != os.getpid()
        assert await supervisor._readiness.wait_agent_ready(
            15 if mode == "slow-shell" else 5
        )
        yield supervisor, process
    finally:
        try:
            await asyncio.wait_for(supervisor.stop(terminate_worker=True), 35)
        finally:
            if process is not None and process.returncode is None:
                await process.stop()
            if process is not None:
                assert process.returncode is not None, (
                    "Owned fault-injection child must be reaped"
                )


@asynccontextmanager
async def _gateway(supervisor, *, send_buffer=None):
    from websockets.asyncio.server import serve

    attached = asyncio.Event()
    gateway = {}

    async def handler(ws):
        if send_buffer is not None:
            ws.transport.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer
            )
        lock = asyncio.Lock()
        gateway.update(ws=ws, lock=lock)
        supervisor.attach_gateway_connection(ws, lock)
        attached.set()
        try:
            await ws.wait_closed()
        finally:
            await supervisor.on_gateway_disconnect(ws, "fault-injection")

    options = (
        {"compression": None, "write_limit": send_buffer}
        if send_buffer is not None
        else {}
    )
    async with serve(handler, "127.0.0.1", 0, **options) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", gateway, attached


def _request(frames=64, size=8192):
    return AgentRequest(
        request_id=uuid.uuid4().hex,
        channel_id="web",
        session_id="ipc-pressure",
        req_method=ReqMethod.CHAT_SEND,
        params={"frames": frames, "bytes": size},
    )


async def _traffic(supervisor, gateway, client, frames):
    async def receive():
        sequence = []
        while True:
            wire = json.loads(await client.recv())
            if wire.get("phase") == "final":
                return sequence
            assert wire.get("response_kind") != "e2a.error", wire
            sequence.append(wire["sequence"])

    request = _request(frames)
    request.params["freeze"] = False
    received, _ = await asyncio.wait_for(
        asyncio.gather(
            receive(),
            supervisor.dispatch_parsed_request(gateway["ws"], request, gateway["lock"]),
        ),
        10,
    )
    assert received == list(range(frames))


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(os.name != "nt", reason="inject a raw Windows pipe frame")
async def test_real_worker_reconnects_after_invalid_frame(tmp_path, monkeypatch):
    from websockets.asyncio.client import connect as ws_connect
    from jiuwenswarm.server.ipc.protocol import HEADER

    async with _worker("pool-pressure", tmp_path, monkeypatch, max_restarts=1) as (supervisor, process):
        pid = supervisor.worker_pid
        original_conn = supervisor._conn
        # Bypass the sender's encoder to reproduce a malformed peer on the OS
        # transport. The Worker must retire this connection and keep Runtime.
        await asyncio.to_thread(original_conn._conn.send_bytes, HEADER.pack(1) + b"[")
        async with asyncio.timeout(10):
            while supervisor._conn is original_conn or supervisor._conn is None:
                await asyncio.sleep(0.01)
            assert await supervisor._readiness.wait_agent_ready(5)
        assert supervisor.worker_pid == pid and process.returncode is None
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                await _traffic(supervisor, gateway, client, 20)
        (tmp_path / "acceptance.json").write_text(json.dumps({
            "case": "malformed_frame_reconnect", "pid": pid, "same_pid": True,
            "ordered_frames_after_reconnect": 20, "stderr": str(process.stderr_path),
        }, indent=2), encoding="utf-8")
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.slow
async def test_real_worker_survives_large_log_regex(tmp_path, monkeypatch, caplog):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("regex-logging", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                dispatch = asyncio.create_task(supervisor.dispatch_parsed_request(
                    gateway["ws"], _request(1), gateway["lock"],
                ))
                first = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert first["phase"] == "regex_starting"
                final = json.loads(await asyncio.wait_for(client.recv(), 5))
                await asyncio.wait_for(dispatch, 2)
                assert final["phase"] == "final" and final["log_seconds"] < 2
                assert supervisor.worker_pid == pid and supervisor._restart_count == 0
                assert process.returncode is None
        assert "heartbeat timed out" not in caplog.text
        (tmp_path / "acceptance.json").write_text(json.dumps({
            "case": "large_log_regex", "pid": pid, "log_seconds": final["log_seconds"],
            "restarts": supervisor._restart_count, "stderr": str(process.stderr_path),
        }, indent=2), encoding="utf-8")
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.slow
async def test_real_worker_survives_blocked_logging_and_exits_before_sink_recovers(
    tmp_path, monkeypatch, caplog,
):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("slow-logging", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                began = time.monotonic()
                dispatch = asyncio.create_task(supervisor.dispatch_parsed_request(
                    gateway["ws"], _request(1), gateway["lock"],
                ))
                first = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert first["phase"] == "logging_pending"
                assert first["first_seconds"] < 2
                assert first["log_thread"] == "worker-log-writer"
                await asyncio.sleep(31)
                assert supervisor._last_rx > began + 25
                assert supervisor._restart_count == 0
                assert supervisor.worker_pid == pid and process.returncode is None
                final = json.loads(await asyncio.wait_for(client.recv(), 10))
                await asyncio.wait_for(dispatch, 2)
                assert final["phase"] == "final" and final["sink_pending"]
                assert final["blocked_seconds"] >= 36
        assert "heartbeat timed out" not in caplog.text
        assert "worker loop unresponsive" not in caplog.text
        exit_started = time.monotonic()
        await supervisor.stop(terminate_worker=True)
        exit_seconds = time.monotonic() - exit_started
        assert process.returncode == 0
        assert exit_seconds < 5
        (tmp_path / "acceptance.json").write_text(json.dumps({
            "case": "blocked_log_sink", "pid": pid, "restarts": supervisor._restart_count,
            "transport_timeout_seconds": 30, "blocked_seconds": final["blocked_seconds"],
            "first_response_seconds": first["first_seconds"], "shutdown_seconds": exit_seconds,
            "exit_code": process.returncode, "stderr": str(process.stderr_path),
        }, indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.slow
async def test_real_worker_survives_busy_pools_timeout_and_reconnect(
    tmp_path, monkeypatch, caplog
):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("pool-pressure", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with _saturated_default_pool() as blockers:
                started = time.monotonic()
                async with ws_connect(url) as client:
                    await asyncio.wait_for(attached.wait(), 2)
                    lock = gateway["lock"]
                    await lock.acquire()
                    dispatch = asyncio.create_task(
                        supervisor.dispatch_parsed_request(
                            gateway["ws"],
                            _request(1),
                            lock,
                        )
                    )
                    try:
                        await asyncio.sleep(11)
                        assert "Gateway forwarding timed out" in caplog.text
                        assert (
                            supervisor._restart_count == 0
                            and process.returncode is None
                        )
                    finally:
                        lock.release()
                    await asyncio.wait_for(dispatch, 3)
                    wire = json.loads(await asyncio.wait_for(client.recv(), 3))
                    assert wire["response_kind"] == "e2a.error"
                    assert "GATEWAY_FORWARD_FAILED" in json.dumps(wire)
                    snapshot = json.loads(
                        supervisor._last_dump_path.read_text(
                            encoding="utf-8"
                        ).splitlines()[0]
                    )
                    assert snapshot["gateway"]["lock_wait_seconds"] >= 9.5
                    assert snapshot["ipc"]["last_tx_age"] is not None
                    assert snapshot["ipc"]["last_rx_age"] is not None
                    await _traffic(supervisor, gateway, client, 200)
                attached.clear()
                async with ws_connect(url) as client:
                    await asyncio.wait_for(attached.wait(), 2)
                    await _traffic(supervisor, gateway, client, 200)
                    await asyncio.sleep(max(0, 36 - (time.monotonic() - started)))
                    assert all(not blocker.done() for blocker in blockers)
                    assert supervisor._last_rx > started + 25
                    assert (
                        supervisor._restart_count == 0 and supervisor.worker_pid == pid
                    )
                    assert process.returncode is None
                    await _traffic(supervisor, gateway, client, 20)
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "case": "busy_pools_and_gateway_timeout",
                    "pid": pid,
                    "busy_seconds": 36,
                    "heartbeat_timeout": 30,
                    "ordered_frames": 420,
                    "reconnect": "passed",
                    "restarts": 0,
                    "stderr": str(process.stderr_path),
                    "diagnostic_dump": str(supervisor._last_dump_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell discovery and pipes")
async def test_real_worker_keeps_heartbeat_during_slow_shell_probe(
    tmp_path, monkeypatch, caplog
):
    """A real model hook must not stall IPC or process exit on a slow OS probe."""
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("slow-shell", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                started = time.monotonic()
                dispatch = asyncio.create_task(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"], _request(1), gateway["lock"]
                    )
                )
                first = json.loads(await asyncio.wait_for(client.recv(), 5))
                assert first["phase"] == "shell_pending"
                assert first["unknown"] and first["probe_pending"]
                assert first["first_round_seconds"] < 2
                assert first["probe_calls"] == first["probe_threads"] == 1
                assert first["probe_daemon"]

                # No business frames are emitted during this interval, so
                # last_rx advancing beyond 25 seconds demonstrates heartbeats.
                await asyncio.sleep(31)
                assert supervisor._last_rx > started + 25
                assert supervisor.worker_pid == pid and process.returncode is None
                assert supervisor._restart_count == 0
                assert supervisor._readiness.state is ReadinessState.AGENT_READY

                final = json.loads(await asyncio.wait_for(client.recv(), 10))
                await asyncio.wait_for(dispatch, 3)
                assert final["phase"] == "final"
                assert final["unknown"] and final["probe_pending"]
                assert final["probe_seconds"] >= 35
                assert final["probe_calls"] == final["probe_threads"] == 1
                assert final["probe_daemon"]
                assert final["rails"] == 12 and final["rounds"] >= 30
                assert supervisor.worker_pid == pid and supervisor._restart_count == 0
                assert "heartbeat timed out" not in caplog.text

        shutdown_started = time.monotonic()
        await asyncio.wait_for(supervisor.stop(terminate_worker=True), 7)
        shutdown_seconds = time.monotonic() - shutdown_started
        assert process.returncode == 0, "Worker must exit normally without termination"
        assert shutdown_seconds < 5
        evidence = {
            "case": "slow_shell_probe",
            "pid": pid,
            "hook": "RuntimePromptRail.before_model_call",
            "first_round_seconds": first["first_round_seconds"],
            "heartbeat_timeout": 30,
            "restarts": 0,
            "terminal_received": True,
            "worker_exit_code": process.returncode,
            "shutdown_seconds": shutdown_seconds,
            "stderr": str(process.stderr_path),
            **final,
        }
    (tmp_path / "acceptance.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows answers heartbeats off-loop; a stalled loop no longer "
    "times out transport (see test_real_freeze_worker_survives_loop_stall)",
)
async def test_real_stalled_worker_dumps_stacks_and_triggers_watchdog(
    tmp_path, monkeypatch, caplog
):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("freeze", tmp_path, monkeypatch) as (supervisor, process):
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                started = time.monotonic()
                dispatch = asyncio.create_task(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"],
                        _request(1),
                        gateway["lock"],
                    )
                )
                assert (
                    json.loads(await asyncio.wait_for(client.recv(), 3))["phase"]
                    == "freezing"
                )
                assert process.returncode is None, (
                    "Fault injection must reach a live hung child"
                )
                await asyncio.wait_for(dispatch, 39)
                assert time.monotonic() - started >= 29.5
                assert "runtime worker heartbeat timed out" in caplog.text
                assert supervisor._readiness.state is ReadinessState.FAILED
                assert await process.wait(timeout=10) is not None, (
                    "A confirmed unhealthy Worker must be retired even without a restart budget"
                )
                failure = json.loads(await asyncio.wait_for(client.recv(), 3))
                assert "WORKER_CLOSED" in json.dumps(failure)
                stderr = process.stderr_path.read_text(encoding="utf-8")
                assert "Timeout (0:00:25)" in stderr
                assert (
                    "diagnostic_worker.py" in stderr
                    and "dispatch_parsed_request" in stderr
                )
                dump = supervisor._last_dump_path.read_text(encoding="utf-8")
                assert "ASYNCIO TASKS" in dump and "THREAD STACKS" in dump
                snapshot = json.loads(dump.splitlines()[0])
                assert snapshot["last_rx_age"] >= 30
                assert snapshot["ipc"]["last_tx_age"] is not None
                assert snapshot["worker_stderr"] == str(process.stderr_path)
                assert "event_loop_lag_seconds" in snapshot
                (tmp_path / "acceptance.json").write_text(
                    json.dumps(
                        {
                            "case": "live_stalled_worker",
                            "pid": supervisor.worker_pid,
                            "worker_exit_code": process.returncode,
                            "watchdog_seconds": time.monotonic() - started,
                            "native_stack_dump": "passed",
                            "async_stack_dump": "passed",
                            "stderr": str(process.stderr_path),
                            "diagnostic_dump": str(supervisor._last_dump_path),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(
    os.name != "nt", reason="Windows named-pipe heartbeat fast path"
)
async def test_real_freeze_worker_survives_loop_stall(tmp_path, monkeypatch, caplog):
    """A blocked business loop keeps transport liveness and must not be killed.

    The freeze fixture blocks the Worker loop for 60 seconds. Off-loop pongs
    carry the growing stall age, so the Supervisor warns instead of executing
    the Worker, and the request completes once the loop resumes.
    """
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("freeze", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                dispatch = asyncio.create_task(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"],
                        _request(1),
                        gateway["lock"],
                    )
                )
                assert (
                    json.loads(await asyncio.wait_for(client.recv(), 3))["phase"]
                    == "freezing"
                )
                started = time.monotonic()
                await asyncio.wait_for(dispatch, 90)
                assert time.monotonic() - started >= 55, "loop really froze"
                assert supervisor.worker_pid == pid
                assert supervisor._restart_count == 0
                assert process.returncode is None
                assert "worker loop unresponsive" in caplog.text
                assert "heartbeat timed out" not in caplog.text
                assert "business loop stalled" not in caplog.text
                (tmp_path / "acceptance.json").write_text(
                    json.dumps(
                        {
                            "case": "transient_loop_stall_survives",
                            "pid": pid,
                            "restarts": supervisor._restart_count,
                            "dispatch": "completed",
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(
    os.name != "nt", reason="Windows named-pipe heartbeat fast path"
)
@pytest.mark.parametrize("max_restarts", [0, 1])
async def test_real_sustained_stall_replaces_worker(
    tmp_path, monkeypatch, caplog, max_restarts
):
    """Sustained stalls stay detectable: pongs report the stall, we replace."""
    from websockets.asyncio.client import connect as ws_connect

    monkeypatch.setattr(supervisor_module, "_WORKER_STALL_RESTART", 40.0)
    async with _worker("freeze", tmp_path, monkeypatch, max_restarts=max_restarts) as (supervisor, process):
        pid = supervisor.worker_pid
        instance = supervisor.worker_instance
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                dispatch = asyncio.create_task(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"],
                        _request(1),
                        gateway["lock"],
                    )
                )
                assert (
                    json.loads(await asyncio.wait_for(client.recv(), 3))["phase"]
                    == "freezing"
                )
                started = time.monotonic()
                await asyncio.wait_for(dispatch, 60)
                assert time.monotonic() - started >= 35
                assert "business loop stalled" in caplog.text
                assert "heartbeat timed out" not in caplog.text
                failure = json.loads(await asyncio.wait_for(client.recv(), 3))
                assert "WORKER_CLOSED" in json.dumps(failure)
                if max_restarts:
                    assert await supervisor._readiness.wait_agent_ready(15)
                    assert supervisor.worker_pid != pid
                    assert supervisor.worker_instance != instance
                    assert process.returncode is not None
                    await _traffic(supervisor, gateway, client, 3)
                else:
                    async with asyncio.timeout(10):
                        while supervisor._readiness.state is not ReadinessState.FAILED:
                            await asyncio.sleep(0.01)
                    assert supervisor._restart_count == 0
                    assert await process.wait(timeout=10) is not None
                (tmp_path / "acceptance.json").write_text(
                    json.dumps({"case": "confirmed_stall_retires_unhealthy_worker",
                                "restart_budget": max_restarts, "old_pid": pid,
                                "old_instance": instance, "old_exit_code": process.returncode,
                                "current_pid": supervisor.worker_pid,
                                "current_instance": supervisor.worker_instance,
                                "ready": supervisor._readiness.state.value,
                                "fresh_request": "passed" if max_restarts else "not admitted"}, indent=2),
                    encoding="utf-8",
                )


@pytest.mark.asyncio
async def test_real_abnormal_exit_keeps_exception_traceback(
    tmp_path, monkeypatch, caplog
):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("crash", tmp_path, monkeypatch) as (supervisor, process):
        async with _gateway(supervisor) as (url, gateway, attached):
            async with ws_connect(url) as client:
                await asyncio.wait_for(attached.wait(), 2)
                await asyncio.wait_for(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"],
                        _request(1),
                        gateway["lock"],
                    ),
                    5,
                )
                assert "WORKER_CLOSED" in await asyncio.wait_for(client.recv(), 3)
        assert await process.wait(timeout=3) == 1
        stderr = process.stderr_path.read_text(encoding="utf-8")
        assert "Traceback" in stderr and "WorkerForceExit" in stderr
        assert "injected diagnostic exit" in stderr
        assert "fatal exit" in stderr
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "case": "abnormal_worker_exit",
                    "exit_code": 1,
                    "traceback": "passed",
                    "stderr": str(process.stderr_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_event_loop_lag_dump_is_bounded_and_omits_queue_payload(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(supervisor_module, "_WATCHDOG_INTERVAL", 0.01)
    monkeypatch.setattr(supervisor_module, "_HEARTBEAT_TIMEOUT", 0.05)
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    supervisor._generation = 1
    supervisor._last_rx = time.monotonic()
    supervisor._process = SimpleNamespace(stderr_path=tmp_path / "worker.stderr.log")
    supervisor._forward_queue = asyncio.Queue()
    await supervisor._forward_queue.put(
        {"payload": "DO_NOT_COPY_BUSINESS_PAYLOAD" * 10000}
    )
    task = asyncio.create_task(supervisor._watchdog(1))
    await asyncio.sleep(0.005)
    time.sleep(0.1)  # A real short scheduler stall; no artificial last_rx offset.
    await asyncio.wait_for(task, 1)
    text = supervisor._last_dump_path.read_text(encoding="utf-8")
    snapshot = json.loads(text.splitlines()[0])
    assert snapshot["event_loop_lag_seconds"] >= 0.08
    assert "ASYNCIO TASKS" in text and "THREAD STACKS" in text
    assert (
        text.index("THREAD STACKS")
        < text.index("in dump_runtime_state")
        < text.index("ASYNCIO TASKS")
    )
    assert "DO_NOT_COPY_BUSINESS_PAYLOAD" not in text
    assert len(text) < 256 * 1024


@pytest.mark.asyncio
@pytest.mark.slow
async def test_real_websocket_send_backpressure_reports_send_timeout(
    tmp_path, monkeypatch, caplog
):
    from websockets.asyncio.client import connect as ws_connect

    async with _worker("pool-pressure", tmp_path, monkeypatch) as (supervisor, process):
        async with _gateway(supervisor, send_buffer=4096) as (url, gateway, attached):
            # Stop consuming after the real client's one-frame receive queue
            # fills. Disable compression so the large frames fill TCP buffers.
            async with ws_connect(url, max_queue=1, compression=None) as client:
                await asyncio.wait_for(attached.wait(), 2)
                dispatch = asyncio.create_task(
                    supervisor.dispatch_parsed_request(
                        gateway["ws"],
                        _request(32, 256 * 1024),
                        gateway["lock"],
                    )
                )
                async with asyncio.timeout(15):
                    while "Gateway forwarding timed out" not in caplog.text:
                        await asyncio.sleep(0.05)
                snapshot = json.loads(
                    supervisor._last_dump_path.read_text(encoding="utf-8").splitlines()[
                        0
                    ]
                )
                assert snapshot["gateway"]["ws_send_seconds"] >= 9.5
                assert snapshot["gateway"]["lock_wait_seconds"] < 0.5
                assert supervisor._restart_count == 0 and process.returncode is None
                assert supervisor._readiness.state is ReadinessState.AGENT_READY
                async with asyncio.timeout(10):
                    while True:
                        wire = json.loads(await client.recv())
                        if wire.get("response_kind") == "e2a.error":
                            assert "GATEWAY_FORWARD_FAILED" in json.dumps(wire)
                            break
                await asyncio.wait_for(dispatch, 3)
                (tmp_path / "acceptance.json").write_text(
                    json.dumps(
                        {
                            "case": "real_websocket_backpressure",
                            "pid": supervisor.worker_pid,
                            "ws_send_seconds": snapshot["gateway"]["ws_send_seconds"],
                            "lock_wait_seconds": snapshot["gateway"][
                                "lock_wait_seconds"
                            ],
                            "worker_alive": True,
                            "restarts": 0,
                            "diagnostic_dump": str(supervisor._last_dump_path),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
