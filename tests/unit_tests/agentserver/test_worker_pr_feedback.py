# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PR feedback: uncertain probes retry and cleanup preserves the primary failure."""
import asyncio
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.ipc import client
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor, shutdown


def transient_error(kind):
    if kind == "busy":
        return BlockingIOError(errno.EAGAIN, "backlog full")
    if kind == "reset":
        return ConnectionResetError(errno.ECONNRESET, "peer reset")
    if kind == "aborted":
        return ConnectionAbortedError(errno.ECONNABORTED, "peer aborted")
    error = OSError("native pipe unavailable")
    error.winerror = kind
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["busy", "reset", "aborted", 121, 231, 232, 233])
async def test_uncertain_probe_is_retryable_and_never_absent(monkeypatch, tmp_path, kind):
    path = tmp_path / "worker"
    path.touch()
    error = transient_error(kind)
    monkeypatch.setattr(client, "connect", AsyncMock(side_effect=error))
    with pytest.raises(TimeoutError, match="temporarily unavailable") as caught:
        await client.try_connect(str(path))
    assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["busy", "reset", 231])
async def test_busy_unleased_current_endpoint_never_spawns_a_replacement(monkeypatch, tmp_path, kind):
    path = tmp_path / "worker"
    path.touch()
    monkeypatch.setattr(client, "connect", AsyncMock(side_effect=transient_error(kind)))
    monkeypatch.setattr(runtime_supervisor, "try_connect", client.try_connect)
    monkeypatch.setattr(runtime_supervisor, "endpoint_is_listening", lambda _: False)
    retry = AsyncMock(side_effect=TimeoutError("busy endpoint retry exhausted"))
    monkeypatch.setattr(runtime_supervisor, "connect_with_retry", retry)
    constructor = Mock(side_effect=AssertionError("Uncertain availability must not spawn"))
    monkeypatch.setattr(runtime_supervisor, "WorkerProcess", constructor)
    owner = runtime_supervisor.RuntimeSupervisor(Readiness(), socket_path=str(path), connect_timeout=1)
    with pytest.raises(TimeoutError, match="retry exhausted"):
        await owner._bring_up()
    retry.assert_awaited_once()
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["busy", "reset", 121, 231])
async def test_current_shutdown_retries_transient_connect_errors(monkeypatch, tmp_path, kind):
    path = tmp_path / "worker"
    path.touch()
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "pid": 123}), close=AsyncMock())
    connect = AsyncMock(side_effect=[transient_error(kind), peer])
    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(shutdown, "try_connect", client.try_connect)
    monkeypatch.setattr(shutdown, "endpoint_is_listening", lambda _: False)
    retire = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(shutdown, "retire_worker", retire)
    result = await shutdown.shutdown_resident_worker(socket_path=str(path), timeout=1)
    assert result == {"ok": True, "pid": 123}
    assert connect.await_count == 2
    retire.assert_awaited_once()
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_shutdown", [False, True])
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_current_close_error_preserves_shutdown_failure(monkeypatch, tmp_path, cancel_shutdown, cancel_close):
    failure = asyncio.CancelledError("shutdown cancelled") if cancel_shutdown else RuntimeError("persistence incomplete")
    close_error = asyncio.CancelledError("close cancelled") if cancel_close else OSError("close failed")
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "pid": 123}),
                           close=AsyncMock(side_effect=close_error))
    monkeypatch.setattr(shutdown, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(shutdown, "retire_worker", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)) as caught:
        await shutdown.shutdown_resident_worker(socket_path=str(tmp_path / "worker"), timeout=1)
    assert caught.value is failure
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_close_failure_after_success_is_still_reported(monkeypatch, tmp_path):
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "pid": 123}),
                           close=AsyncMock(side_effect=OSError("close failed")))
    monkeypatch.setattr(shutdown, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(shutdown, "retire_worker", AsyncMock(return_value={"ok": True}))
    with pytest.raises(OSError, match="close failed"):
        await shutdown.shutdown_resident_worker(socket_path=str(tmp_path / "worker"), timeout=1)
