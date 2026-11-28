# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Supervisor worker lifecycle spans and dedup rules."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.runtime_trace.recorder import _REASON_CODES
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


class _FakeRecorder:
    def __init__(self) -> None:
        self.spans = []

    def start(self, name, *, level="normal", parent=None, mono_start=None, attributes=None):
        return {
            "name": name,
            "level": level,
            "attributes": dict(attributes or {}),
        }

    def end(self, handle, outcome, *, mono_end=None, attributes=None):
        handle["outcome"] = outcome
        handle["end_attributes"] = dict(attributes or {})
        self.spans.append(handle)

    def event(self, handle, name, attributes=None):
        return None


def _supervisor(recorder=None, *, max_restarts: int = 3) -> RuntimeSupervisor:
    supervisor = RuntimeSupervisor(
        Readiness(),
        socket_path="test.sock",
        max_restarts=max_restarts,
        recorder=recorder,
    )
    supervisor._worker_pid = 4242
    supervisor._worker_instance = "instance"
    supervisor._current_launch = "a" * 32
    supervisor._peer_generation = 2
    supervisor._generation = 1
    return supervisor


def _by_name(recorder: _FakeRecorder, name: str) -> list[dict]:
    return [span for span in recorder.spans if span["name"] == name]


def test_exited_owned_process_requires_process_handle() -> None:
    supervisor = _supervisor(_FakeRecorder())
    supervisor._owns_process = True
    supervisor._process = None
    assert supervisor._exited_owned_process() is False


def test_terminal_dedup_allows_exited_after_ipc_lost() -> None:
    supervisor = _supervisor(_FakeRecorder())
    assert supervisor._claim_terminal("worker.ipc_lost") is True
    assert supervisor._claim_terminal("worker.ipc_lost") is False
    # exit codes must still be recorded after an EOF was seen first
    assert supervisor._claim_terminal("worker.exited") is True


def test_worker_exit_records_exit_code_once() -> None:
    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._trace_worker_exit(4242, "a" * 32, 7)
    supervisor._trace_worker_exit(4242, "a" * 32, 7)
    spans = _by_name(recorder, "worker.exited")
    assert len(spans) == 1
    assert spans[0]["level"] == "terminal"
    assert spans[0]["attributes"]["runtime.exit_code"] == 7
    assert spans[0]["attributes"]["runtime.worker_pid"] == 4242


def test_worker_failed_dedups_by_generation() -> None:
    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._apply_readiness(
        {"state": "FAILED", "reason": "boom"}
    )
    supervisor._apply_readiness(
        {"state": "FAILED", "reason": "boom again"}
    )
    assert len(_by_name(recorder, "worker.failed")) == 1

    supervisor._peer_generation = 3
    supervisor._apply_readiness({"state": "FAILED", "reason": "new generation"})
    assert len(_by_name(recorder, "worker.failed")) == 2


@pytest.mark.asyncio
async def test_schedule_restart_budget_exhausted() -> None:
    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder, max_restarts=0)
    assert await supervisor._schedule_restart("boom", reason_code="bring_up_failed") is False
    spans = _by_name(recorder, "worker.restart")
    assert len(spans) == 1
    assert spans[0]["outcome"] == "error"
    assert spans[0]["end_attributes"]["runtime.reason_code"] == "budget_exhausted"
    assert spans[0]["attributes"]["runtime.worker_pid"] == 4242


@pytest.mark.asyncio
async def test_schedule_restart_records_attempt_and_snapshot() -> None:
    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    assert await supervisor._schedule_restart("closed", reason_code="ipc_eof") is True
    span = _by_name(recorder, "worker.restart")[0]
    assert span["attributes"]["runtime.attempt"] == 1
    assert span["attributes"]["runtime.budget"] == 3
    assert span["attributes"]["runtime.backoff_ms"] == 200
    assert span["attributes"]["runtime.launch"] == "a" * 32
    assert span["end_attributes"]["runtime.reason_code"] == "ipc_eof"


@pytest.mark.asyncio
async def test_pump_records_ipc_lost_when_process_still_alive() -> None:
    class _Conn:
        async def recv(self):
            raise OSError("eof")

    class _Process:
        returncode = None

    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._conn = _Conn()
    supervisor._owns_process = True
    supervisor._process = _Process()
    with pytest.raises(OSError):
        await supervisor._pump()
    spans = _by_name(recorder, "worker.ipc_lost")
    assert len(spans) == 1
    assert spans[0]["end_attributes"]["runtime.reason_code"] == "ipc_eof"


@pytest.mark.asyncio
async def test_pump_does_not_steal_exit_evidence() -> None:
    class _Conn:
        async def recv(self):
            raise OSError("eof")

    class _ExitedProcess:
        returncode = 1

    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._conn = _Conn()
    supervisor._owns_process = True
    supervisor._process = _ExitedProcess()
    with pytest.raises(OSError):
        await supervisor._pump()
    assert _by_name(recorder, "worker.ipc_lost") == []


@pytest.mark.asyncio
async def test_pump_ignores_attached_worker() -> None:
    class _Conn:
        async def recv(self):
            raise OSError("eof")

    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._conn = _Conn()
    supervisor._owns_process = False
    supervisor._process = None
    with pytest.raises(OSError):
        await supervisor._pump()
    # A reattached resident Worker has no process handle, so EOF is its only
    # terminal evidence and must be recorded.
    assert len(_by_name(recorder, "worker.ipc_lost")) == 1


@pytest.mark.asyncio
async def test_stop_spawned_process_clears_stale_identity() -> None:
    supervisor = _supervisor(_FakeRecorder())
    supervisor._process = None
    await supervisor._stop_spawned_process()
    assert supervisor._current_launch is None
    assert supervisor._worker_pid is None
    assert supervisor._worker_instance is None
    assert supervisor._peer_generation is None


def test_all_reason_codes_are_stable_enums() -> None:
    recorder = _FakeRecorder()
    supervisor = _supervisor(recorder)
    supervisor._trace_worker_exit(4242, "a" * 32, 0)
    supervisor._apply_readiness({"state": "FAILED", "reason": "x"})
    for span in recorder.spans:
        reason = span["end_attributes"].get("runtime.reason_code")
        if reason is not None:
            assert reason in _REASON_CODES
