# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async byte stream for one IPC connection.

POSIX uses a Unix domain socket. Windows uses a named pipe via the stdlib
``multiprocessing.connection`` transport, bridged onto the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from jiuwenswarm.server.ipc.protocol import (
    HEADER,
    IpcError,
    MAX_FRAME_BYTES,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger(__name__)

_PIPE_AUTHKEY = b"jiuwenswarm-runtime-ipc-v1"


def endpoint_address(path: str) -> str:
    """Return the OS address for a logical socket path."""
    if os.name != "nt":
        return path
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:20]
    return rf"\\.\pipe\jiuwenswarm-{digest}"


class IpcConnection:
    """One bidirectional IPC session."""

    def __init__(self) -> None:
        self._last_tx = self._last_rx = 0.0
        self._send_seconds = self._recv_seconds = self._write_lock_seconds = 0.0
        self._sending_since = self._receiving_since = 0.0
        self._write_waiters: dict[object, float] = {}

    @asynccontextmanager
    async def _locked_write(self):
        started = time.monotonic()
        marker = object()
        self._write_waiters[marker] = started
        try:
            async with self._write_lock:
                self._write_waiters.pop(marker)
                self._write_lock_seconds = time.monotonic() - started
                if self._closed:
                    raise ConnectionError("ipc connection is closed")
                self._sending_since = time.monotonic()
                try:
                    yield
                finally:
                    self._send_seconds = time.monotonic() - self._sending_since
                    self._sending_since = 0.0
        finally:
            self._write_waiters.pop(marker, None)

    def diagnostics(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "last_tx_age": now - self._last_tx if self._last_tx else None,
            "last_rx_age": now - self._last_rx if self._last_rx else None,
            "send_seconds": now - self._sending_since
            if self._sending_since
            else self._send_seconds,
            "recv_seconds": now - self._receiving_since
            if self._receiving_since
            else self._recv_seconds,
            "write_lock_seconds": now - min(self._write_waiters.values())
            if self._write_waiters
            else self._write_lock_seconds,
        }

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def recv(self) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StreamConnection(IpcConnection):
    """Length-prefixed frames over an ``asyncio`` stream."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._locked_write():
            self._writer.write(frame)
            await self._writer.drain()
            self._last_tx = time.monotonic()

    async def recv(self) -> dict[str, Any]:
        self._receiving_since = time.monotonic()
        try:
            result = await self._recv_frame()
            self._last_rx = time.monotonic()
            return result
        finally:
            self._recv_seconds = time.monotonic() - self._receiving_since
            self._receiving_since = 0.0

    async def _recv_frame(self) -> dict[str, Any]:
        try:
            header = await self._reader.readexactly(HEADER.size)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("ipc connection closed") from exc
        (length,) = HEADER.unpack(header)
        if length > MAX_FRAME_BYTES:
            raise IpcError(f"ipc frame is {length} bytes; limit is {MAX_FRAME_BYTES}")
        try:
            body = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("ipc connection closed") from exc
        return decode_frame(body)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            logger.debug("[IPC] writer close failed", exc_info=True)


class PipeConnection(IpcConnection):
    """Named-pipe connection. ``send_bytes`` supplies its own framing."""

    def __init__(self, conn: Any) -> None:
        super().__init__()
        self._conn = conn
        self._write_lock = asyncio.Lock()
        self._closed = False
        # A blocking read must not occupy the thread needed to send, and
        # Runtime tools must not starve either direction in the default pool.
        self._reader_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-ipc-read"
        )
        self._writer_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-ipc-write"
        )

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._locked_write():
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._writer_pool, self._conn.send_bytes, frame
                )
                self._last_tx = time.monotonic()
            except asyncio.CancelledError:
                # The native write can outlive the cancelled coroutine.
                # Abort it before another send can reuse the same pipe.
                await self.close()
                raise

    async def recv(self) -> dict[str, Any]:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        self._receiving_since = time.monotonic()
        try:
            frame = await asyncio.get_running_loop().run_in_executor(
                self._reader_pool, self._conn.recv_bytes
            )
            self._last_rx = time.monotonic()
        except asyncio.CancelledError:
            await self.close()
            raise
        except EOFError as exc:
            await self.close()
            raise ConnectionError("ipc connection closed") from exc
        except OSError as exc:
            await self.close()
            raise ConnectionError("ipc connection closed") from exc
        finally:
            self._recv_seconds = time.monotonic() - self._receiving_since
            self._receiving_since = 0.0
        header_size = HEADER.size
        if len(frame) < header_size:
            raise IpcError("ipc pipe frame is truncated")
        (length,) = HEADER.unpack(frame[:header_size])
        body = frame[header_size:]
        if length != len(body):
            raise IpcError("ipc pipe frame length mismatch")
        return decode_frame(body)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._conn, "close", None)
        try:
            if callable(close):
                # CloseHandle cancels pending native I/O and does not need
                # a default-pool thread (which may be entirely occupied).
                close()
        finally:
            self._reader_pool.shutdown(wait=False, cancel_futures=True)
            self._writer_pool.shutdown(wait=False, cancel_futures=True)
