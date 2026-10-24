# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shutting down the resident Runtime Worker over IPC."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.supervisor.worker_process import request_resident_worker_shutdown

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix socket endpoint")


def _socket_path() -> str:
    return f"/tmp/jwrs-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"


@pytest.mark.asyncio
async def test_request_shutdown_returns_none_without_worker() -> None:
    assert await request_resident_worker_shutdown(_socket_path()) is None


@pytest.mark.asyncio
async def test_request_shutdown_sends_shutdown_and_returns_pid() -> None:
    path = _socket_path()
    server = IpcServer(path)
    await server.start()

    async def _serve() -> dict:
        conn = await server.accept()
        try:
            await conn.send({"kind": KIND_HELLO, "protocol": 1, "pid": 4321, "generation": 1})
            return await conn.recv()
        finally:
            await conn.close()

    serve_task = asyncio.create_task(_serve())
    try:
        assert await request_resident_worker_shutdown(path) == 4321
        received = await asyncio.wait_for(serve_task, 2)
        assert received["kind"] == KIND_SHUTDOWN
    finally:
        await server.close()


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
