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
from typing import Any

from jiuwenswarm.server.ipc.protocol import HEADER, IpcError, MAX_FRAME_BYTES, decode_frame, encode_frame

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

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def recv(self) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StreamConnection(IpcConnection):
    """Length-prefixed frames over an ``asyncio`` stream."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._write_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def recv(self) -> dict[str, Any]:
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
        self._conn = conn
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._write_lock:
            await asyncio.to_thread(self._conn.send_bytes, frame)

    async def recv(self) -> dict[str, Any]:
        try:
            frame = await asyncio.to_thread(self._conn.recv_bytes)
        except EOFError as exc:
            raise ConnectionError("ipc connection closed") from exc
        except OSError as exc:
            raise ConnectionError("ipc connection closed") from exc
        if len(frame) < HEADER.size:
            raise IpcError("ipc pipe frame is truncated")
        (length,) = HEADER.unpack(frame[: HEADER.size])
        body = frame[HEADER.size:]
        if length != len(body):
            raise IpcError("ipc pipe frame length mismatch")
        return decode_frame(body)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._conn, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
