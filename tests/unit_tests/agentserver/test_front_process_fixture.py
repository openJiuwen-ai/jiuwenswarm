# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front subprocess readiness must tolerate cold startup and diagnose failures."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.unit_tests.agentserver.fixtures import front_process


@pytest.mark.asyncio
async def test_front_waits_past_fixed_retry_count(monkeypatch, tmp_path):
    ws = SimpleNamespace(
        recv=AsyncMock(return_value=json.dumps({"event": "connection.ack"})),
        close=AsyncMock(),
    )
    connect = AsyncMock(side_effect=[ConnectionRefusedError()] * 401 + [TimeoutError(), ws])
    monkeypatch.setattr(front_process, "connect", connect)
    monkeypatch.setattr(front_process.asyncio, "sleep", AsyncMock())
    process = SimpleNamespace(pid=123, poll=Mock(return_value=None))

    actual = await front_process.connect_front(process, 12345, tmp_path / "front.log")

    assert actual is ws
    assert connect.await_count == 403
    ws.recv.assert_awaited_once()
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_front_reports_early_exit_and_startup_log(monkeypatch, tmp_path):
    log = tmp_path / "front.log"
    log.write_text("ModuleNotFoundError: startup_dependency\n", encoding="utf-8")
    process = SimpleNamespace(pid=123, poll=Mock(side_effect=[None, 1, 1]))
    connect = AsyncMock(side_effect=ConnectionRefusedError())
    monkeypatch.setattr(front_process, "connect", connect)
    monkeypatch.setattr(front_process.asyncio, "sleep", AsyncMock())

    with pytest.raises(AssertionError) as failure:
        await front_process.connect_front(process, 12345, log)

    message = str(failure.value)
    assert "exit_code=1" in message
    assert "pid=123" in message
    assert "ws://127.0.0.1:12345" in message
    assert "ModuleNotFoundError: startup_dependency" in message
    connect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "connection.ack"])
async def test_front_bounds_connection_and_ack_wait(monkeypatch, tmp_path, phase):
    async def never_ready(*args, **kwargs):
        await asyncio.Event().wait()

    ws = SimpleNamespace(recv=AsyncMock(side_effect=never_ready), close=AsyncMock())
    connect = AsyncMock(side_effect=never_ready) if phase == "connect" else AsyncMock(return_value=ws)
    monkeypatch.setattr(front_process, "connect", connect)
    process = SimpleNamespace(pid=123, poll=Mock(return_value=None))

    with pytest.raises(AssertionError, match=f"Front startup failed during {phase}") as failure:
        await front_process.connect_front(process, 12345, tmp_path / "front.log", timeout=.05, ack_timeout=.1)

    assert "exit_code=None" in str(failure.value)
    assert "<missing>" in str(failure.value)
    assert isinstance(failure.value.__cause__, TimeoutError)
    assert f"timeout={.05 if phase == 'connect' else .1}s" in str(failure.value)
    if phase == "connection.ack":
        ws.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", ["not-json", json.dumps({"event": "unexpected"})])
async def test_front_rejects_invalid_ack_and_closes_connection(monkeypatch, tmp_path, acknowledgement):
    ws = SimpleNamespace(recv=AsyncMock(return_value=acknowledgement), close=AsyncMock())
    monkeypatch.setattr(front_process, "connect", AsyncMock(return_value=ws))
    process = SimpleNamespace(pid=123, poll=Mock(return_value=None))

    with pytest.raises(AssertionError, match="Front startup failed during connection.ack"):
        await front_process.connect_front(process, 12345, tmp_path / "front.log")

    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_front_cancellation_closes_unacknowledged_connection(monkeypatch, tmp_path):
    waiting = asyncio.Event()

    async def pending_ack():
        waiting.set()
        await asyncio.Event().wait()

    ws = SimpleNamespace(recv=AsyncMock(side_effect=pending_ack), close=AsyncMock())
    monkeypatch.setattr(front_process, "connect", AsyncMock(return_value=ws))
    process = SimpleNamespace(pid=123, poll=Mock(return_value=None))
    task = asyncio.create_task(front_process.connect_front(process, 12345, tmp_path / "front.log"))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    ws.close.assert_awaited_once()
