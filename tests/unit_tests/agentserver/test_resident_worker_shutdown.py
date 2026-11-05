# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shutting down the resident Runtime Worker over IPC."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.supervisor.worker_process import request_resident_worker_shutdown

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix socket endpoint")


def _socket_path() -> str:
    return f"/tmp/jwrs-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"


@pytest.mark.asyncio
async def test_request_shutdown_returns_none_without_worker() -> None:
    assert await request_resident_worker_shutdown(_socket_path()) is None


@pytest.mark.asyncio
async def test_request_shutdown_sends_shutdown_and_returns_pid(monkeypatch) -> None:
    from pathlib import Path
    from jiuwenswarm.server.supervisor.worker_process import WorkerProcess
    from jiuwenswarm.server.ipc.client import connect_with_retry

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    path = _socket_path()
    worker = WorkerProcess(path, host="127.0.0.1", port=0, stub=True)
    connection = None
    try:
        await worker.spawn()
        connection = await connect_with_retry(path, timeout=15)
        hello = await connection.recv()
        await connection.close()
        connection = None
        assert await request_resident_worker_shutdown(path) == hello["pid"]
        assert await worker.wait(timeout=1) == 0
    finally:
        if connection is not None:
            await connection.close()
        await worker.stop(timeout=2)


@pytest.mark.asyncio
async def test_request_shutdown_rejects_non_worker_endpoint() -> None:
    path = _socket_path()
    server = IpcServer(path)
    await server.start()

    async def _serve() -> None:
        conn = await server.accept()
        try:
            await conn.send({"kind": "something-else"})
        finally:
            await conn.close()

    serve_task = asyncio.create_task(_serve())
    try:
        with pytest.raises(RuntimeError):
            await request_resident_worker_shutdown(path)
        await asyncio.wait_for(serve_task, 2)
    finally:
        await server.close()
