# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IPC client owned by the Runtime Supervisor."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

from jiuwenswarm.server.ipc.stream import (
    _PIPE_AUTHKEY,
    IpcConnection,
    PipeConnection,
    StreamConnection,
    endpoint_address,
)

_PIPE_CONNECT_POOL = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="runtime-ipc-connect"
)


async def connect(path: str) -> IpcConnection:
    if os.name == "nt":
        return await _connect_pipe(path)
    reader, writer = await asyncio.open_unix_connection(path)
    return StreamConnection(reader, writer)


async def _connect_pipe(path: str) -> IpcConnection:
    from multiprocessing.connection import Client

    pending = _PIPE_CONNECT_POOL.submit(Client, endpoint_address(path), "AF_PIPE", _PIPE_AUTHKEY)
    connecting = asyncio.wrap_future(pending)
    try:
        raw = await asyncio.shield(connecting)
    except asyncio.CancelledError:
        # Native Client cannot be cancelled once running. Its completion must
        # close the handle even after this Front's event loop has closed.
        connecting.cancel()

        def close_late(future):
            if not future.cancelled() and future.exception() is None:
                future.result().close()

        pending.add_done_callback(close_late)
        raise
    return PipeConnection(raw)


def endpoint_is_listening(path: str) -> bool:
    """Return True when a Worker has already bound this endpoint.

    POSIX checks the lifetime owner lease. Windows named pipes are not files, so this
    waits briefly for a pipe instance without connecting to it.
    """
    if os.path.exists(path + ".owner.lock"):
        # The lifetime lease also detects a busy Windows pipe instance, for
        # which WaitNamedPipeW returns false while its Front is disconnecting.
        from filelock import FileLock, Timeout
        lease = FileLock(path + ".owner.lock", timeout=0)
        try:
            with lease:
                return False
        except Timeout:
            return True
    if os.name != "nt":
        return False
    return _windows_pipe_is_listening(endpoint_address(path))


def _windows_pipe_is_listening(address: str) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wait = kernel32.WaitNamedPipeW
    wait.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    wait.restype = ctypes.c_int
    if wait(address, 1):
        return True
    # Old Workers have no lifetime lease. A busy legacy pipe still exists,
    # although WaitNamedPipe cannot offer a free instance to this Front yet.
    return ctypes.get_last_error() in (121, 231)


async def try_connect(path: str, *, timeout: float = 0.5) -> IpcConnection | None:
    """Return None only for an absent/refused endpoint; timeout is not absence."""
    if os.name != "nt" and not os.path.exists(path):
        return None
    try:
        return await asyncio.wait_for(connect(path), timeout=timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        return None


async def connect_with_retry(path: str, *, timeout: float) -> IpcConnection:
    """Wait until the spawned Worker binds and accepts."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await asyncio.wait_for(connect(path), timeout=max(.001, deadline - time.monotonic()))
        except (OSError, asyncio.TimeoutError) as exc:
            last_error = exc
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"runtime worker ipc not ready: {path}") from last_error
