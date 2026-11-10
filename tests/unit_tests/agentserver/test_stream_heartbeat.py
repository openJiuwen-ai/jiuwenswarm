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
from jiuwenswarm.server.ipc.protocol import HEADER, MAX_FRAME_BYTES, IpcError, KIND_HEARTBEAT, encode_frame
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.worker.diagnostics import LoopBeat
from jiuwenswarm.server.worker.service import _install_heartbeat_fast_path
from jiuwenswarm.server.worker.stall_context import BusinessTracker

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _pair(transport, tmp_path):
    if transport == "unix":
        if os.name == "nt":
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
        assert worker._loop is not asyncio.get_running_loop()
        yield worker, peer
    finally:
        await worker.close()
        await peer.close()
        if source is not None:
            await source.close()
        await server.close()
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
