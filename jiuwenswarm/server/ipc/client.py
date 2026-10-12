# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IPC client owned by the Runtime Supervisor."""

from __future__ import annotations

import asyncio
import os
import time

from jiuwenswarm.server.ipc.stream import (
    _PIPE_AUTHKEY,
    IpcConnection,
    PipeConnection,
    StreamConnection,
    endpoint_address,
)


async def connect(path: str) -> IpcConnection:
    if os.name == "nt":
        return await _connect_pipe(path)
    reader, writer = await asyncio.open_unix_connection(path)
    return StreamConnection(reader, writer)


async def _connect_pipe(path: str) -> IpcConnection:
    from multiprocessing.connection import Client

    address = endpoint_address(path)
    raw = await asyncio.to_thread(Client, address, "AF_PIPE", _PIPE_AUTHKEY)
    return PipeConnection(raw)


def endpoint_is_listening(path: str) -> bool:
    """Return True when a Worker has already bound this endpoint.

    POSIX checks the socket file. Windows named pipes are not files, so this
    waits briefly for a pipe instance without connecting to it.
    """
    if os.name != "nt":
        return os.path.exists(path)
    return _windows_pipe_is_listening(endpoint_address(path))


def _windows_pipe_is_listening(address: str) -> bool:
    import ctypes

    wait = ctypes.windll.kernel32.WaitNamedPipeW
    wait.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    wait.restype = ctypes.c_int
    return bool(wait(address, 1))


async def try_connect(path: str, *, timeout: float = 0.5) -> IpcConnection | None:
    """Return a connection when a Worker is already listening, else ``None``."""
    if os.name != "nt" and not os.path.exists(path):
        return None
    try:
        return await asyncio.wait_for(connect(path), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None


async def connect_with_retry(path: str, *, timeout: float) -> IpcConnection:
    """Wait until the spawned Worker binds and accepts."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await connect(path)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise TimeoutError(f"runtime worker ipc not ready: {path}") from last_error
