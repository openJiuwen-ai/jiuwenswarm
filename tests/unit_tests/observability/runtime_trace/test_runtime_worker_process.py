# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for WorkerProcess spawn tracing and TRACEPARENT injection."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.supervisor import worker_process
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess

_TRACE_ID = 0x11111111111111111111111111111111
_SPAN_ID = 0x2222222222222222


class _Span:
    def get_span_context(self):
        return SimpleNamespace(
            is_valid=True,
            trace_id=_TRACE_ID,
            span_id=_SPAN_ID,
            trace_flags=1,
        )


class _Handle:
    def __init__(self, name, level, attributes) -> None:
        self.name = name
        self.level = level
        self.span = _Span()
        self.attributes = attributes


class _Recorder:
    def __init__(self) -> None:
        self.spans = []

    def start(self, name, *, level="normal", parent=None, mono_start=None, attributes=None):
        return _Handle(name, level, dict(attributes or {}))

    def end(self, handle, outcome, *, mono_end=None, attributes=None):
        self.spans.append(
            {
                "name": handle.name,
                "level": handle.level,
                "attributes": handle.attributes,
                "outcome": outcome,
                "end_attributes": dict(attributes or {}),
            }
        )


class _Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.mark.asyncio
async def test_spawn_injects_traceparent_from_spawn_span(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = _Recorder()
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _Proc(4321)

    monkeypatch.setattr(worker_process.subprocess, "Popen", fake_popen)
    process = WorkerProcess(
        str(tmp_path / "worker.sock"),
        host="127.0.0.1",
        port=18092,
        recorder=recorder,
    )
    assert re.fullmatch(r"[0-9a-f]{32}", process.launch)
    await process.spawn()

    assert captured["env"]["TRACEPARENT"] == (
        "00-11111111111111111111111111111111-2222222222222222-01"
    )
    span = recorder.spans[0]
    assert span["name"] == "worker.spawn"
    assert span["level"] == "event"
    assert span["outcome"] == "success"
    assert span["attributes"]["runtime.launch"] == process.launch
    assert span["end_attributes"]["runtime.worker_pid"] == 4321


@pytest.mark.asyncio
async def test_spawn_failure_records_bring_up_failed(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = _Recorder()

    def broken_popen(command, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(worker_process.subprocess, "Popen", broken_popen)
    process = WorkerProcess(
        str(tmp_path / "worker.sock"),
        host="127.0.0.1",
        port=18092,
        recorder=recorder,
    )
    with pytest.raises(OSError):
        await process.spawn()
    span = recorder.spans[0]
    assert span["outcome"] == "error"
    assert span["end_attributes"]["runtime.reason_code"] == "bring_up_failed"


@pytest.mark.asyncio
async def test_spawn_without_recorder_keeps_environment(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc(1)

    monkeypatch.setattr(worker_process.subprocess, "Popen", fake_popen)
    process = WorkerProcess(
        str(tmp_path / "worker.sock"), host="127.0.0.1", port=18092
    )
    await process.spawn()
    assert "TRACEPARENT" not in captured["env"]
