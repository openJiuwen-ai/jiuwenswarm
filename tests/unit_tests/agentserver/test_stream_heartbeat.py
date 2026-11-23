# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Socket heartbeat isolation, bounded buffering and business attribution."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager

import pytest

from jiuwenswarm.server.ipc import stream
from jiuwenswarm.server.ipc.client import connect
from jiuwenswarm.server.ipc.io_loop import ipc_io
from jiuwenswarm.server.ipc.loopback import FrontConnectSessionSource, FrontLoopbackListener, LoopbackParameters
from jiuwenswarm.server.ipc.protocol import (
    HEADER, MAX_FRAME_BYTES, IpcError, KIND_HEARTBEAT, KIND_REQUEST, KIND_REQUEST_DONE, encode_frame,
)
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.worker.diagnostics import LoopBeat
from jiuwenswarm.server.worker.service import _install_heartbeat_fast_path
from jiuwenswarm.server.worker.stall_context import BusinessTracker

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _pair(transport, tmp_path):
    if transport in {"unix", "resident"}:
        if transport == "unix" and os.name == "nt":
            pytest.skip("asyncio Unix domain sockets require POSIX")
        server = IpcServer(f"/tmp/jw-hb-{os.getpid()}-{time.time_ns()}.sock")
        await server.start()
        worker = await connect(server.path)
        peer = await server.accept()
        source = None
    else:
        server = FrontLoopbackListener()
        await server.start(code_identity="a" * 64, data_scope="b" * 64)
        source = FrontConnectSessionSource(LoopbackParameters.from_payload(
            server.launch_payload(), code_identity="a" * 64, data_scope="b" * 64,
        ))
        await source.start()
        worker = await source.accept()
        peer = await server.accept()
    try:
        if isinstance(worker, stream.StreamConnection):
            assert worker._loop is not asyncio.get_running_loop()
        yield worker, peer
    finally:
        await worker.close()
        await peer.close()
        if source is not None:
            await source.close()
        await server.close()
        if isinstance(worker, stream.StreamConnection):
            assert worker._reader_task is None or worker._reader_task.done()


@pytest.mark.parametrize("transport", ["tcp", "unix"])
@pytest.mark.parametrize("observed_age", [10, 26])
async def test_socket_pong_survives_business_frame_then_blocked_loop(transport, observed_age, tmp_path):
    async with _pair(transport, tmp_path) as (worker, peer):
        beat = LoopBeat()
        beat.enable_tracking()
        # Attribution must already be present in pongs before the 30s warning.
        beat._last -= observed_age
        _install_heartbeat_fast_path(worker, beat)
        await peer.send({"kind": "wire", "id": "first"})
        assert (await worker.recv())["id"] == "first"
        received = threading.Event()

        async def exchange():
            await asyncio.sleep(0.05)
            await peer.send({"kind": "wire", "id": "queued"})
            await peer.send({"kind": KIND_HEARTBEAT})
            reply = await asyncio.wait_for(peer.recv(), 1)
            received.set()
            return reply

        async def blocking_work():
            time.sleep(0.3)
            assert received.is_set(), "pong must arrive before the business loop resumes"

        async def innocent_work():
            await asyncio.sleep(0.5)

        innocent = None
        try:
            with beat.activity({"operation": "request", "request_id": "innocent"}):
                innocent = asyncio.create_task(innocent_work(), name="innocent")
            response = asyncio.run_coroutine_threadsafe(exchange(), peer._loop)
            with beat.activity({"operation": "request", "request_id": "blocking",
                                "session_id": "s-blocking", "method": "designer.run.execute"}):
                task = asyncio.create_task(blocking_work(), name="blocking-business")
            await task
            reply = await asyncio.wrap_future(response)
            assert reply["loop_beat_age"] >= observed_age
            context = reply["loop_stall"]
            assert context["request_id"] == "blocking"
            assert context["session_id"] == "s-blocking"
            assert context["task"] == "blocking-business"
            assert any(frame["function"] == "blocking_work" for frame in context["stack"])
            assert (await worker.recv())["id"] == "queued"
        finally:
            if innocent is not None:
                innocent.cancel()
                await asyncio.gather(innocent, return_exceptions=True)
            beat.close_tracking()


@pytest.mark.parametrize("transport", ["tcp", "unix"])
@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_socket_backlog_is_bounded_and_preserves_frames(transport, limit, tmp_path, monkeypatch, caplog):
    frame = {"kind": "wire", "id": "queued"}
    if limit == "messages":
        monkeypatch.setattr(stream, "_STREAM_PENDING_MESSAGES", 3)
    else:
        monkeypatch.setattr(stream, "_STREAM_PENDING_BYTES", 3 * len(encode_frame(frame)))
    async with _pair(transport, tmp_path) as (worker, peer):
        await peer.send(frame)
        assert await worker.recv() == frame
        for _ in range(4):
            await peer.send(frame)
        async with asyncio.timeout(2):
            while worker._reader_error is None:
                await asyncio.sleep(0.005)
        assert worker.diagnostics()["receive_pending"] == 3
        for _ in range(3):
            assert await worker.recv() == frame
        with pytest.raises(IpcError, match="receive backlog exceeded"):
            await worker.recv()
        assert "business receive backlog exceeded; transport=stream" in caplog.text
        assert "pending_messages=3" in caplog.text
        await worker.close()
        assert worker.diagnostics()["receive_pending_bytes"] == 0


@pytest.mark.parametrize("transport", ["tcp", "unix"])
async def test_cancelled_socket_receive_keeps_reader_available(transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, peer):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.recv(), 0.02)
        await peer.send({"kind": "wire", "id": "after-cancel"})
        assert (await asyncio.wait_for(worker.recv(), 1))["id"] == "after-cancel"


@pytest.mark.parametrize("transport", ["tcp", "unix"])
async def test_socket_protocol_error_follows_already_received_business_frames(transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, peer):
        await peer.send({"kind": "wire", "id": "initial"})
        await worker.recv()

        @ipc_io
        async def corrupt():
            peer._writer.write(encode_frame({"kind": "wire", "id": "queued"}))
            peer._writer.write(HEADER.pack(MAX_FRAME_BYTES + 1))
            await peer._writer.drain()

        await corrupt()
        assert (await asyncio.wait_for(worker.recv(), 1))["id"] == "queued"
        with pytest.raises(IpcError, match="limit"):
            await asyncio.wait_for(worker.recv(), 1)


async def test_business_tracker_preserves_custom_task_factory_and_explicit_context():
    import contextvars

    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()
    seen = []

    def factory(owner, coro, **kwargs):
        seen.append(True)
        return asyncio.Task(coro, loop=owner, **kwargs)

    loop.set_task_factory(factory)
    tracker = BusinessTracker()
    try:
        with tracker.activity({"operation": "request", "request_id": "inherited"}):
            context = contextvars.copy_context()
        task = asyncio.create_task(asyncio.sleep(0), context=context)
        await task
        assert tracker._tasks[task]["request_id"] == "inherited"
        assert seen
        tracker.close()
        assert loop.get_task_factory() is factory
    finally:
        tracker.close()
        loop.set_task_factory(original)


async def test_socket_diagnostics_remain_available_while_writers_wait(tmp_path):
    async with _pair("tcp", tmp_path) as (worker, peer):
        queued = threading.Event()
        release = threading.Event()

        @ipc_io
        async def hold_write_lock():
            async with worker._write_lock:
                queued.set()
                while not release.is_set():
                    await asyncio.sleep(0.001)

        holder = asyncio.create_task(hold_write_lock())
        senders = []
        try:
            async with asyncio.timeout(2):
                while not queued.is_set():
                    await asyncio.sleep(0.001)
            senders = [asyncio.create_task(worker.send({"kind": "wire", "index": i})) for i in range(100)]
            async with asyncio.timeout(2):
                while not worker._write_waiters:
                    await asyncio.sleep(0.001)
            assert worker.diagnostics()["write_lock_seconds"] >= 0
            release.set()
            while not all(task.done() for task in senders):
                worker.diagnostics()
                await asyncio.sleep(0)
            await asyncio.gather(*senders)
            assert [(await peer.recv())["index"] for _ in range(100)] == list(range(100))
            assert worker.diagnostics()["write_lock_seconds"] >= 0
        finally:
            release.set()
            await asyncio.gather(holder, *senders, return_exceptions=True)

@pytest.mark.parametrize("reply", ["matched", "legacy", "stale", "silent", "business"])
async def test_front_resumes_with_one_bounded_probe_on_real_socket(reply, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_WATCHDOG_INTERVAL", 0.02)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_TIMEOUT", 0.08)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_INTERVAL", 0.12)
    supervisor = runtime_supervisor.RuntimeSupervisor(Readiness())
    probes = []
    async with _pair("tcp", tmp_path) as (worker, front):
        if reply == "matched":
            _install_heartbeat_fast_path(worker, LoopBeat())
        responder = worker._heartbeat_responder

        def respond(frame):
            probes.append(frame["probe_id"])
            if responder:
                responder(frame)
            elif reply == "legacy":
                worker.send_direct({"kind": KIND_HEARTBEAT})
            elif reply == "stale":
                worker.send_direct({"kind": KIND_HEARTBEAT, "probe_id": "from-earlier-probe"})

        worker.set_heartbeat_responder(respond)
        # Start both real I/O readers. The Front pump deliberately does no
        # further recv: already queued frames must not masquerade as fresh traffic.
        await worker.send({"kind": KIND_HEARTBEAT, "probe_id": "old"})
        assert (await front.recv())["probe_id"] == "old"
        supervisor._conn = front
        supervisor._peer_generation = supervisor._generation = 1
        supervisor._peer_echoes_probe = reply != "legacy"
        supervisor._last_rx = time.monotonic()
        reader = asyncio.create_task(worker.recv())
        watchdog = asyncio.create_task(supervisor._watchdog(1))
        blocked_writer = False
        try:
            await asyncio.sleep(0)
            start_of_stall = time.monotonic()
            time.sleep(0.14)  # suspend the actual loop which owns the watchdog
            if reply == "business":
                request = {"kind": KIND_REQUEST, "generation": 1, "dispatch_id": "post-resume"}
                async def write_before_watchdog_resumes():
                    await front.send(request)
                    await front._write_lock.acquire()

                asyncio.run_coroutine_threadsafe(write_before_watchdog_resumes(), front._loop).result(1)
                blocked_writer = True
                assert await asyncio.wait_for(reader, 1) == request
                async with asyncio.timeout(1):
                    while front._probe_observer is None:
                        await asyncio.sleep(0)
                await worker.send({**request, "kind": KIND_REQUEST_DONE})
            async with asyncio.timeout(2):
                if reply == "business":
                    while supervisor._last_rx <= start_of_stall:
                        await asyncio.sleep(0.005)
                    assert not front._closed and not probes
                elif reply in ("matched", "legacy"):
                    while not probes or supervisor._last_rx <= start_of_stall:
                        await asyncio.sleep(0.005)
                    assert not front._closed
                else:
                    await watchdog
                    assert front._closed and supervisor._unhealthy_reason == "runtime worker heartbeat timed out"
            assert len(probes) == (0 if blocked_writer else 1)
        finally:
            if blocked_writer:
                front._loop.call_soon_threadsafe(front._write_lock.release)
            supervisor._stopping = True
            reader.cancel()
            watchdog.cancel()
            await asyncio.gather(reader, watchdog, return_exceptions=True)

@pytest.mark.parametrize("transport", ["tcp", "resident"])
async def test_probe_on_closed_connection_reports_disconnection(transport, tmp_path):
    async with _pair(transport, tmp_path) as (_worker, front):
        await front.close()
        result = await front.probe_heartbeat(1, correlated=True)
        assert result.state == "connection_closed"
        assert result.frame is None
        assert front._probe_observer is None and not front._probe_lock.locked()


@pytest.mark.parametrize("transport", ["tcp", "resident"])
@pytest.mark.parametrize("failure", ["connection", "reader", "write", "runtime"])
async def test_probe_send_failure_preserves_disconnect_classification(transport, failure, tmp_path, monkeypatch):
    async with _pair(transport, tmp_path) as (_worker, front):
        async def fail_send(_message):
            if failure == "connection":
                raise ConnectionError("peer disconnected before reader noticed")
            if failure == "reader":
                front._reader_error = ConnectionError("peer closed during write")
            if failure == "runtime":
                raise RuntimeError("controlled writer failure")
            raise OSError("controlled writer failure")

        monkeypatch.setattr(front, "send", fail_send)
        result = await front.probe_heartbeat(1, correlated=True)
        expected = "connection_closed" if failure in {"connection", "reader"} else "write_failed"
        assert result.state == expected
        assert result.frame is None
        assert front._probe_observer is None and not front._probe_lock.locked()


@pytest.mark.parametrize("transport", ["tcp", "resident"])
async def test_failed_probe_write_cannot_use_uncorrelated_legacy_heartbeat(transport, tmp_path, monkeypatch):
    async with _pair(transport, tmp_path) as (_worker, front):
        async def fail_after_incoming_heartbeat(_message):
            front._record_frame({"kind": KIND_HEARTBEAT})
            raise OSError("probe write did not complete")

        monkeypatch.setattr(front, "send", fail_after_incoming_heartbeat)
        result = await front.probe_heartbeat(1, correlated=False)
        assert result.state == "write_failed" and result.frame is None


async def test_probe_write_deadline_and_cancelled_token_on_real_socket(tmp_path):
    async with _pair("tcp", tmp_path) as (worker, front):
        await stream.on_loop(front._loop, front._write_lock.acquire())
        try:
            blocked = await asyncio.wait_for(front.probe_heartbeat(0.05, correlated=True), 1)
            assert blocked.state == "write_timeout"
        finally:
            front._loop.call_soon_threadsafe(front._write_lock.release)

    # A native write that had already begun when the first connection timed out
    # can still finish during its teardown. Use a new physical generation.
    async with _pair("tcp", tmp_path) as (worker, front):
        reader = asyncio.create_task(front.recv())
        first = asyncio.create_task(front.probe_heartbeat(2, correlated=True))
        tasks = [reader, first]
        try:
            old = await asyncio.wait_for(worker.recv(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(front.probe_heartbeat(1, correlated=True))
            tasks.append(second)
            current = await asyncio.wait_for(worker.recv(), 1)
            assert old["probe_id"] != current["probe_id"]
            await worker.send(old)
            assert (await asyncio.wait_for(reader, 1))["probe_id"] == old["probe_id"]
            assert not second.done()
            await worker.send(current)
            result = await asyncio.wait_for(second, 2)
            assert result.state == "received" and result.frame["probe_id"] == current["probe_id"]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

@pytest.mark.parametrize("transport", ["tcp", "resident"])
@pytest.mark.parametrize("case", [
    "fresh", "stale", "unknown", "wrong_generation", "duplicate", "disconnected",
    "write_queued", "other_connection", "legacy_heartbeat_first",
])
async def test_backpressured_probe_requires_a_fresh_business_round_trip(case, transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        generation = 7
        request = {"kind": KIND_REQUEST, "dispatch_id": "unique-business-request", "generation": generation}
        done = {**request, "kind": KIND_REQUEST_DONE}
        since = time.monotonic()
        if case == "other_connection":
            async with _pair(transport, tmp_path) as (other_worker, other_front):
                await other_front.send(request)
                assert await other_worker.recv() == request
        elif case != "write_queued":
            await front.send(request)
            assert await worker.recv() == request
        if case == "stale":
            since = time.monotonic()
        if case == "duplicate":
            await worker.send(done)
            assert await front.recv() == done
            # The original matching completion is valid recent evidence. Only
            # the duplicate arriving after this new cutoff must be rejected.
            since = time.monotonic()

        owner = front._loop if isinstance(front, stream.StreamConnection) else asyncio.get_running_loop()
        await stream.on_loop(owner, front._write_lock.acquire())
        reader = asyncio.create_task(front.recv())
        probe = asyncio.create_task(front.probe_heartbeat(
            0.15, correlated=case != "legacy_heartbeat_first", request_since=since, generation=generation,
        ))
        queued = asyncio.create_task(front.send(request)) if case == "write_queued" else None
        try:
            async with asyncio.timeout(1):
                while not front._write_waiters:
                    await asyncio.sleep(0.001)
            if case == "unknown":
                done["dispatch_id"] = "never-issued"
            if case == "wrong_generation":
                done["generation"] -= 1
            if case == "legacy_heartbeat_first":
                await worker.send({"kind": KIND_HEARTBEAT})
                assert (await asyncio.wait_for(reader, 1))["kind"] == KIND_HEARTBEAT
                reader = asyncio.create_task(front.recv())
            await worker.send(done)
            assert await asyncio.wait_for(reader, 1) == done
            if case == "disconnected":
                await worker.close()
                async with asyncio.timeout(1):
                    while front._reader_error is None:
                        await asyncio.sleep(0.005)
            result = await asyncio.wait_for(probe, 1)
            if case in {"fresh", "legacy_heartbeat_first"}:
                assert result.state == "business_reply" and result.frame == done
                # The write is still waiting for the async lock and never entered
                # a native send. Its deadline must also release the queued task.
                async with asyncio.timeout(0.3):
                    while front._probe_write is not None:
                        await asyncio.sleep(0.001)
                again = await front.probe_heartbeat(0.04, correlated=True, request_since=since, generation=generation)
                assert again.state == "write_timeout"
            elif case == "disconnected":
                assert result.state == "connection_closed" and result.frame is None
            else:
                assert result.state == "write_timeout" and result.frame is None
        finally:
            if queued is not None:
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
            owner.call_soon_threadsafe(front._write_lock.release)
            probe.cancel()
            reader.cancel()
            await asyncio.gather(probe, reader, return_exceptions=True)


async def test_proven_native_pipe_write_does_not_queue_another_probe_or_outlive_close():
    entered, release = threading.Event(), threading.Event()
    writes = []

    class NativePipe:
        def send_bytes(self, frame):
            writes.append(frame)
            if len(writes) > 1:
                entered.set()
                assert release.wait(3)

        def close(self):
            release.set()

    front = stream.PipeConnection(NativePipe())
    request = {"kind": KIND_REQUEST, "dispatch_id": "current", "generation": 4}
    since = time.monotonic()
    try:
        await front.send(request)
        probe = asyncio.create_task(front.probe_heartbeat(
            0.12, correlated=True, request_since=since, generation=4,
        ))
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        await asyncio.to_thread(front._record_frame, {**request, "kind": KIND_REQUEST_DONE})
        assert (await probe).state == "business_reply"
        native_write = front._probe_write
        assert native_write is not None and not native_write.done()
        again = await front.probe_heartbeat(0.04, correlated=True, request_since=since, generation=4)
        assert again.state == "write_timeout" and front._probe_write is native_write
        assert len(writes) == 2
        await front.close()
        async with asyncio.timeout(1):
            await asyncio.gather(native_write, return_exceptions=True)
        assert front._probe_write is None and front._closed and release.is_set()
    finally:
        release.set()
        await front.close()


@pytest.mark.parametrize("transport", ["tcp", "resident"])
@pytest.mark.parametrize("waiting", ["reply", "cancel", "timeout"])
async def test_overlapping_heartbeat_probes_own_their_observation(transport, waiting, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        reader = asyncio.create_task(front.recv())
        first = asyncio.create_task(front.probe_heartbeat(1.5, correlated=True))
        tasks = [first, reader]
        try:
            first_ping = await asyncio.wait_for(worker.recv(), 1)
            second = asyncio.create_task(front.probe_heartbeat(
                0.06 if waiting == "timeout" else 1.5, correlated=True,
            ))
            tasks.append(second)
            await asyncio.sleep(0)
            if waiting == "cancel":
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
            elif waiting == "timeout":
                result = await asyncio.wait_for(second, 0.5)
                assert result.state == "write_timeout" and result.frame is None
            await worker.send(first_ping)
            first_result = await asyncio.wait_for(first, 2)
            assert first_result.state == "received" and first_result.frame == first_ping
            if waiting == "reply":
                second_ping = await asyncio.wait_for(worker.recv(), 1)
                assert first_ping["probe_id"] != second_ping["probe_id"]
                await worker.send(second_ping)
                second_result = await asyncio.wait_for(second, 2)
                assert second_result.state == "received" and second_result.frame == second_ping
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("transport", ["tcp", "resident"])
@pytest.mark.parametrize("recovers", [True, False])
async def test_local_probe_write_pressure_has_one_bounded_grace(transport, recovers, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_WATCHDOG_INTERVAL", 0.10)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_TIMEOUT", 0.16)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_INTERVAL", 0.18)
    supervisor = runtime_supervisor.RuntimeSupervisor(Readiness())
    async with _pair(transport, tmp_path) as (worker, front):
        _install_heartbeat_fast_path(worker, LoopBeat())
        await worker.send({"kind": "started"})
        assert await front.recv() == {"kind": "started"}
        supervisor._conn = front
        supervisor._peer_generation = supervisor._generation = 1
        supervisor._peer_echoes_probe = True
        supervisor._last_rx = time.monotonic()
        entered = asyncio.Event()
        verdict = asyncio.get_running_loop().create_future()
        original_probe = front.probe_heartbeat

        async def record_probe(*args, **kwargs):
            entered.set()
            result = await original_probe(*args, **kwargs)
            verdict.set_result(result)
            return result

        monkeypatch.setattr(front, "probe_heartbeat", record_probe)
        release = threading.Event()
        native = isinstance(front, stream.PipeConnection)
        if native:
            original_write = front._send_bytes_locked

            def blocked_native(*args):
                assert release.wait(3)
                original_write(*args)

            monkeypatch.setattr(front, "_send_bytes_locked", blocked_native)
        else:
            await stream.on_loop(front._loop, front._write_lock.acquire())
        reader = asyncio.create_task(worker.recv())
        watchdog = asyncio.create_task(supervisor._watchdog(1))
        try:
            await asyncio.sleep(0)
            time.sleep(0.36)
            await asyncio.wait_for(entered.wait(), 1)
            if recovers:
                await asyncio.sleep(0.21)
                if native:
                    release.set()
                else:
                    front._loop.call_soon_threadsafe(front._write_lock.release)
            result = await asyncio.wait_for(verdict, 0.8)
            if recovers:
                assert result.state == "received" and result.frame.get("probe_id")
                assert not front._closed and not supervisor._unhealthy_reason
            else:
                await asyncio.wait_for(watchdog, 1)
                assert result.state == "write_timeout" and front._closed
                assert supervisor._unhealthy_reason == "runtime worker IPC probe write timed out"
        finally:
            release.set()
            if not native and not recovers:
                front._loop.call_soon_threadsafe(front._write_lock.release)
            supervisor._stopping = True
            reader.cancel()
            watchdog.cancel()
            await asyncio.gather(reader, watchdog, return_exceptions=True)


@pytest.mark.parametrize("transport", ["tcp", "resident"])
async def test_probe_does_not_extend_an_unanswered_successful_write(transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        probe = asyncio.create_task(front.probe_heartbeat(0.12, correlated=True, write_grace=2))
        assert (await asyncio.wait_for(worker.recv(), 1))["probe_id"]
        result = await asyncio.wait_for(probe, 0.7)
        assert result.state == "reply_timeout" and result.frame is None


@pytest.mark.parametrize("transport", ["tcp", "resident"])
async def test_cancelling_probe_queued_in_grace_preserves_connection(transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        owner = front._loop if isinstance(front, stream.StreamConnection) else asyncio.get_running_loop()
        await stream.on_loop(owner, front._write_lock.acquire())
        first = asyncio.create_task(front.probe_heartbeat(0.05, correlated=True, write_grace=2))
        try:
            async with asyncio.timeout(1):
                while not front._write_waiters:
                    await asyncio.sleep(0.001)
            await asyncio.sleep(0.10)
            assert not first.done()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        finally:
            owner.call_soon_threadsafe(front._write_lock.release)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        assert not front._closed
        reader = asyncio.create_task(front.recv())
        second = asyncio.create_task(front.probe_heartbeat(1, correlated=True))
        try:
            ping = await asyncio.wait_for(worker.recv(), 1)
            await worker.send(ping)
            assert (await asyncio.wait_for(second, 1)).frame == ping
        finally:
            reader.cancel()
            second.cancel()
            await asyncio.gather(reader, second, return_exceptions=True)
