# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Receive backlog and pre-probe completions retain their transport chronology."""

import asyncio
import time

import pytest

from jiuwenswarm.server.ipc import stream
from jiuwenswarm.server.ipc.protocol import KIND_HEARTBEAT, KIND_REQUEST, KIND_REQUEST_DONE
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.worker.diagnostics import LoopBeat
from jiuwenswarm.server.worker.service import _install_heartbeat_fast_path
from tests.unit_tests.agentserver.test_stream_heartbeat import _pair

pytestmark = pytest.mark.asyncio


async def test_queued_heartbeat_cannot_overwrite_newer_business_liveness():
    supervisor = runtime_supervisor.RuntimeSupervisor(Readiness())
    supervisor._note_worker_beat({"kind": KIND_REQUEST_DONE}, received_at=100.0)
    supervisor._note_worker_beat({"kind": KIND_HEARTBEAT, "loop_beat_age": 60.0}, received_at=90.0)
    assert supervisor._worker_last_beat == 100.0
    # A newer transport observation can reveal that the business loop stalled;
    # do not force the derived business beat itself to be monotonic.
    supervisor._note_worker_beat({"kind": KIND_HEARTBEAT, "loop_beat_age": 60.0}, received_at=110.0)
    assert supervisor._worker_last_beat == 50.0


@pytest.mark.parametrize("transport", ["tcp", "resident"])
async def test_completion_received_before_probe_proves_round_trip_once(transport, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        since = time.monotonic()
        request = {"kind": KIND_REQUEST, "generation": 3, "dispatch_id": "completed-before-observer"}
        await front.send(request)
        assert await worker.recv() == request
        done = {**request, "kind": KIND_REQUEST_DONE}
        await worker.send(done)
        assert await front.recv() == done
        owner = front._loop if isinstance(front, stream.StreamConnection) else asyncio.get_running_loop()
        await stream.on_loop(owner, front._write_lock.acquire())
        try:
            result = await front.probe_heartbeat(0.06, correlated=True, request_since=since, generation=3)
            assert result.state == "business_reply" and result.frame == done
            assert result.received_at >= since
            again = await front.probe_heartbeat(0.03, correlated=True, request_since=since, generation=3)
            assert again.state == "write_timeout" and again.frame is None
        finally:
            owner.call_soon_threadsafe(front._write_lock.release)


@pytest.mark.parametrize("transport", ["tcp", "resident"])
@pytest.mark.parametrize("invalid", ["old_request", "old_completion", "wrong_generation", "unknown"])
async def test_preprobe_completion_must_match_current_fresh_request(transport, invalid, tmp_path):
    async with _pair(transport, tmp_path) as (worker, front):
        since = time.monotonic()
        request = {"kind": KIND_REQUEST, "generation": 3, "dispatch_id": "verified"}
        if invalid != "unknown":
            await front.send(request)
            assert await worker.recv() == request
        if invalid == "old_request":
            since = time.monotonic()
        done = {**request, "kind": KIND_REQUEST_DONE}
        if invalid == "wrong_generation":
            done["generation"] = 2
        await worker.send(done)
        assert await front.recv() == done
        if invalid == "old_completion":
            since = time.monotonic()
        owner = front._loop if isinstance(front, stream.StreamConnection) else asyncio.get_running_loop()
        await stream.on_loop(owner, front._write_lock.acquire())
        try:
            result = await front.probe_heartbeat(0.03, correlated=True, request_since=since, generation=3)
            assert result.state == "write_timeout" and result.frame is None
        finally:
            owner.call_soon_threadsafe(front._write_lock.release)


async def test_two_old_queued_pongs_cannot_skip_recovery_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_supervisor, "_WATCHDOG_INTERVAL", 0.04)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_TIMEOUT", 0.12)
    monkeypatch.setattr(runtime_supervisor, "_HEARTBEAT_INTERVAL", 0.16)
    async with _pair("tcp", tmp_path) as (worker, front):
        _install_heartbeat_fast_path(worker, LoopBeat())
        await front.send({"kind": "start-reader"})
        assert await worker.recv() == {"kind": "start-reader"}
        await worker.send({"kind": "start-reader"})
        assert await front.recv() == {"kind": "start-reader"}
        for index in range(2):
            await worker.send({"kind": KIND_HEARTBEAT, "generation": 1, "queued": index})
        async with asyncio.timeout(1):
            while front.diagnostics()["receive_pending"] != 2:
                await asyncio.sleep(0.001)

        async def stalled_front():
            supervisor = runtime_supervisor.RuntimeSupervisor(Readiness())
            supervisor._conn = front
            supervisor._generation = supervisor._peer_generation = 1
            supervisor._peer_echoes_probe = True
            supervisor._last_rx = time.monotonic()
            first, release = asyncio.Event(), asyncio.Event()
            probes = []
            handle, probe = supervisor._handle, front.probe_heartbeat

            async def hold_first(message, **kwargs):
                await handle(message, **kwargs)
                if message.get("queued") == 0:
                    first.set()
                    await release.wait()

            async def record_probe(*args, **kwargs):
                probes.append(await probe(*args, **kwargs))
                return probes[-1]

            monkeypatch.setattr(supervisor, "_handle", hold_first)
            monkeypatch.setattr(supervisor, "_log_diagnostics", lambda _reason: None)
            monkeypatch.setattr(front, "probe_heartbeat", record_probe)
            watchdog = asyncio.create_task(supervisor._watchdog(1))
            pump = None
            try:
                await asyncio.sleep(0)
                time.sleep(0.3)
                pump = asyncio.create_task(supervisor._pump())
                await first.wait()
                # Dequeue the two old frames on opposite sides of the first
                # overdue watchdog check, without sending any fresh heartbeat.
                async with asyncio.timeout(1):
                    while supervisor._event_loop_lag < 0.04:
                        await asyncio.sleep(0)
                release.set()
                async with asyncio.timeout(1):
                    while not probes and not front._closed:
                        await asyncio.sleep(0.005)
                assert not front._closed, "queued old pongs suppressed the probe and killed a healthy Worker"
                assert probes and probes[0].state == "received"
            finally:
                release.set()
                supervisor._stopping = True
                tasks = [watchdog] + ([pump] if pump is not None else [])
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await stream.on_loop(front._loop, stalled_front())
