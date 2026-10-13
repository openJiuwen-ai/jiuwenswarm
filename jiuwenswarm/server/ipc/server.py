# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IPC listener owned by the Runtime Worker.

The socket is single-user. On POSIX it is mode 0600, and on Linux a peer
whose uid does not match this process is rejected. Windows named pipes use
the listener authkey. This is the desktop/single-user host boundary, not a
multi-user access-control system.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from jiuwenswarm.server.ipc.stream import (
    _PIPE_AUTHKEY,
    IpcConnection,
    PipeConnection,
    StreamConnection,
    endpoint_address,
)

logger = logging.getLogger(__name__)


def _restrict_runtime_dir(parent: Path) -> None:
    """Tighten ``~/.jiuwenswarm/run`` when the socket lives there."""
    home = os.environ.get("JIUWENSWARM_HOME") or os.path.expanduser("~")
    run_dir = Path(home) / ".jiuwenswarm" / "run"
    if parent != run_dir:
        return
    try:
        os.chmod(parent, 0o700)
    except OSError:
        logger.warning("[IPC] failed to restrict runtime directory: %s", parent)


def _peer_uid_allowed(sock: object) -> bool:
    """Allow the connection when peer credentials are unavailable or match."""
    uid = _peer_uid(sock)
    if uid is None:
        return True
    return uid == os.getuid()


def _peer_uid(sock: object) -> int | None:
    import socket
    import struct

    getsockopt = getattr(sock, "getsockopt", None)
    if not callable(getsockopt):
        return None
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        raw = getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


class IpcServer:
    """Accept supervisor connections on a Unix socket or a Windows named pipe."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._queue: asyncio.Queue[IpcConnection] = asyncio.Queue()
        self._unix: asyncio.AbstractServer | None = None
        self._listener: object | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if os.name == "nt":
            await self._start_pipe()
            return
        await self._start_unix()

    async def _start_unix(self) -> None:
        target = Path(self.path)
        if len(str(target)) >= 100:
            raise OSError(f"unix socket path is too long: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        _restrict_runtime_dir(target.parent)
        if target.exists():
            target.unlink()
        previous_umask = os.umask(0o077)
        try:
            self._unix = await asyncio.start_unix_server(self._on_unix, path=str(target))
        finally:
            os.umask(previous_umask)
        try:
            os.chmod(target, 0o600)
        except OSError:
            logger.warning("[IPC] failed to restrict socket permissions: %s", target)
        logger.info("[IPC] listening on unix socket %s", target)

    async def _start_pipe(self) -> None:
        from multiprocessing.connection import Listener

        address = endpoint_address(self.path)
        self._listener = Listener(address, family="AF_PIPE", authkey=_PIPE_AUTHKEY)
        self._thread = threading.Thread(
            target=self._accept_pipe, name="runtime-ipc-pipe", daemon=True
        )
        self._thread.start()
        logger.info("[IPC] listening on named pipe %s", address)

    def _accept_pipe(self) -> None:
        listener = self._listener
        loop = self._loop
        if listener is None or loop is None:
            return
        accept = getattr(listener, "accept", None)
        while not self._closed and callable(accept):
            try:
                raw = accept()
            except (OSError, EOFError):
                return
            if self._closed:
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
                return
            conn = PipeConnection(raw)
            asyncio.run_coroutine_threadsafe(self._queue.put(conn), loop)

    async def _on_unix(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("socket")
        if not _peer_uid_allowed(peer):
            logger.warning("[IPC] rejected connection from a different user")
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        await self._queue.put(StreamConnection(reader, writer))

    async def accept(self) -> IpcConnection:
        return await self._queue.get()

    async def close(self) -> None:
        self._closed = True
        if self._unix is not None:
            self._unix.close()
            await self._unix.wait_closed()
            self._unix = None
        listener = self._listener
        if listener is not None:
            close = getattr(listener, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
            self._listener = None
        if os.name != "nt":
            try:
                Path(self.path).unlink()
            except OSError:
                pass
