# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async byte stream for one IPC connection.

Resident POSIX uses a Unix domain socket; Front-attached mode uses loopback
TCP on every platform. Windows resident mode uses a named pipe via the stdlib
``multiprocessing.connection`` transport, bridged onto the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Callable

from jiuwenswarm.server.ipc.io_loop import on_loop
from jiuwenswarm.server.ipc.protocol import (
    HEADER,
    IpcError,
    KIND_HEARTBEAT,
    MAX_FRAME_BYTES,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger(__name__)

_PIPE_AUTHKEY = b"jiuwenswarm-runtime-ipc-v1"
_PIPE_PENDING_MESSAGES = 256
_PIPE_PENDING_BYTES = 2 * MAX_FRAME_BYTES
_STREAM_PENDING_MESSAGES = 256
_STREAM_PENDING_BYTES = 2 * MAX_FRAME_BYTES


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
        self._stats_lock = threading.Lock()
        self._heartbeat_responder: Callable[[dict[str, Any]], None] | None = None

    def set_heartbeat_responder(
        self, responder: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Answer heartbeat pings without the event loop where supported.

        Named pipes use a reader thread; socket connections use their owner
        I/O loop. Neither responder needs the Runtime business loop.
        """
        self._heartbeat_responder = responder

    @asynccontextmanager
    async def _locked_write(self):
        started = time.monotonic()
        marker = object()
        with self._stats_lock:
            self._write_waiters[marker] = started
        try:
            async with self._write_lock:
                with self._stats_lock:
                    self._write_waiters.pop(marker)
                    self._write_lock_seconds = time.monotonic() - started
                    if self._closed:
                        raise ConnectionError("ipc connection is closed")
                    self._sending_since = time.monotonic()
                try:
                    yield
                finally:
                    with self._stats_lock:
                        self._send_seconds = time.monotonic() - self._sending_since
                        self._sending_since = 0.0
        finally:
            with self._stats_lock:
                self._write_waiters.pop(marker, None)

    def diagnostics(self) -> dict[str, Any]:
        # Socket writers run on the I/O thread, while Supervisor diagnostics
        # run on its caller loop. Snapshot under the same lock as mutations.
        with self._stats_lock:
            now = time.monotonic()
            waiters = tuple(self._write_waiters.values())
            return {
                "last_tx_age": now - self._last_tx if self._last_tx else None,
                "last_rx_age": now - self._last_rx if self._last_rx else None,
                "send_seconds": now - self._sending_since
                if self._sending_since
                else self._send_seconds,
                "recv_seconds": now - self._receiving_since
                if self._receiving_since
                else self._recv_seconds,
                "write_lock_seconds": now - min(waiters) if waiters else self._write_lock_seconds,
            }

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def recv(self) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StreamConnection(IpcConnection):
    """Length-prefixed frames owned entirely by their creating I/O loop."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._pending: deque[tuple[dict[str, Any], int]] = deque()
        self._pending_bytes = 0
        self._pending_ready = asyncio.Event()
        self._receive_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._reader_error: Exception | None = None
        self._last_frame_bytes = 0

    async def send(self, message: dict[str, Any]) -> None:
        await on_loop(self._loop, self._send(message))

    async def _send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._locked_write():
            self._writer.write(frame)
            await self._writer.drain()
            self._last_tx = time.monotonic()

    def send_direct(self, message: dict[str, Any]) -> None:
        """Write one complete pong on the I/O loop, without awaiting Runtime.

        Both normal writes and pongs execute on the same thread and write an
        entire frame before yielding. A business coroutine awaiting drain
        therefore cannot interleave frame bytes with this write.
        """
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("direct stream writes require the owner I/O loop")
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        self._writer.write(encode_frame(message))
        self._last_tx = time.monotonic()

    async def recv(self) -> dict[str, Any]:
        return await on_loop(self._loop, self._recv())

    async def _recv(self) -> dict[str, Any]:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="runtime-ipc-stream-read")
        self._receiving_since = time.monotonic()
        try:
            async with self._receive_lock:
                while True:
                    if self._closed:
                        raise ConnectionError("ipc connection is closed")
                    if self._pending:
                        result, size = self._pending.popleft()
                        self._pending_bytes -= size
                        return result
                    if self._reader_error is not None:
                        raise self._reader_error
                    self._pending_ready.clear()
                    await self._pending_ready.wait()
        finally:
            self._recv_seconds = time.monotonic() - self._receiving_since
            self._receiving_since = 0.0

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                message = await self._recv_frame()
                self._last_rx = time.monotonic()
                responder = self._heartbeat_responder
                if responder is not None and message.get("kind") == KIND_HEARTBEAT:
                    responder(message)
                    continue
                size = self._last_frame_bytes
                if (len(self._pending) >= _STREAM_PENDING_MESSAGES
                        or self._pending_bytes + size > _STREAM_PENDING_BYTES):
                    logger.warning(
                        "[IPC] business receive backlog exceeded; transport=stream "
                        "pending_messages=%s pending_bytes=%s incoming_bytes=%s "
                        "limit_messages=%s limit_bytes=%s",
                        len(self._pending), self._pending_bytes, size,
                        _STREAM_PENDING_MESSAGES, _STREAM_PENDING_BYTES,
                    )
                    raise IpcError("ipc business receive backlog exceeded")
                self._pending.append((message, size))
                self._pending_bytes += size
                self._pending_ready.set()
        except Exception as exc:  # noqa: BLE001 - recv owns transport/protocol errors
            self._reader_error = exc
        finally:
            self._pending_ready.set()

    def diagnostics(self) -> dict[str, Any]:
        snapshot = super().diagnostics()
        snapshot["receive_pending"] = len(self._pending)
        snapshot["receive_pending_bytes"] = self._pending_bytes
        return snapshot

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
        self._last_frame_bytes = HEADER.size + length
        return decode_frame(body)

    async def close(self) -> None:
        await on_loop(self._loop, self._close())

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._pending_bytes = 0
        self._pending_ready.set()
        self._writer.close()
        task = self._reader_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
        # The reader thread answers heartbeats while the loop is blocked, so
        # both directions must serialize raw writes through one mutex.
        self._send_mutex = threading.Lock()
        self._read_native_lock = threading.Lock()
        self._closed = False
        self._close_task: asyncio.Task | None = None
        # A blocking read must not occupy the thread needed to send, and
        # Runtime tools must not starve either direction in the default pool.
        self._reader_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-ipc-read"
        )
        self._writer_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-ipc-write"
        )
        self._receive_lock = asyncio.Lock()
        self._receive_mutex = threading.Lock()
        self._pending: deque[tuple[dict[str, Any], int]] = deque()
        self._pending_bytes = 0
        self._reader_started = False
        self._reader_error: Exception | None = None
        self._read_waiter: asyncio.Future[None] | None = None
        self._read_notified = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._locked_write():
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._writer_pool, self._send_bytes_locked, frame
                )
                self._last_tx = time.monotonic()
            except asyncio.CancelledError:
                # The native write can outlive the cancelled coroutine.
                # Abort it before another send can reuse the same pipe.
                await self.close()
                raise

    def _send_bytes_locked(self, frame: bytes) -> None:
        with self._send_mutex:
            if self._closed:
                raise ConnectionError("ipc connection is closed")
            self._conn.send_bytes(frame)

    def send_direct(self, message: dict[str, Any]) -> None:
        """Blocking thread-safe send for the reader-thread heartbeat path."""
        self._send_bytes_locked(encode_frame(message))

    def _read_loop(self) -> None:
        """Keep draining the pipe even between business-loop recv calls.

        Returning after one business message strands subsequent heartbeats
        when session initialization stalls the event loop. Retain ordered,
        bounded business messages here while answering pings on this thread.
        """
        try:
            while not self._closed:
                # Closing a stdlib pipe does not cancel every pending native
                # read. Bound idle waits so shutdown can join this executor.
                with self._read_native_lock:
                    if self._closed:
                        return
                    if not self._conn.poll(0.1):
                        continue
                    if self._closed:
                        return
                    frame = self._conn.recv_bytes(maxlength=MAX_FRAME_BYTES + HEADER.size)
                self._last_rx = time.monotonic()
                message = self._decode_pipe_frame(frame)
                responder = self._heartbeat_responder
                if responder is not None and message.get("kind") == KIND_HEARTBEAT:
                    responder(message)
                    continue
                with self._receive_mutex:
                    if self._closed:
                        return
                    if (len(self._pending) >= _PIPE_PENDING_MESSAGES
                            or self._pending_bytes + len(frame) > _PIPE_PENDING_BYTES):
                        raise IpcError("ipc business receive backlog exceeded")
                    self._pending.append((message, len(frame)))
                    self._pending_bytes += len(frame)
                self._notify_reader()
        except Exception as exc:  # noqa: BLE001 - deliver native and protocol errors to recv
            with self._receive_mutex:
                self._reader_error = exc
            self._notify_reader()

    def _notify_reader(self) -> None:
        with self._receive_mutex:
            loop = self._loop
            if loop is None or self._read_waiter is None or self._read_notified:
                return
            self._read_notified = True
        try:
            loop.call_soon_threadsafe(self._wake_reader)
        except RuntimeError:
            # Connection teardown can finish after its event loop has closed.
            with self._receive_mutex:
                self._read_notified = False

    def _wake_reader(self) -> None:
        with self._receive_mutex:
            self._read_notified = False
            waiter = self._read_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def _next_message(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if not self._reader_started:
            self._loop = loop
            self._reader_started = True
            self._reader_pool.submit(self._read_loop)
        while True:
            with self._receive_mutex:
                if self._closed:
                    raise ConnectionError("ipc connection is closed")
                if self._pending:
                    message, size = self._pending.popleft()
                    self._pending_bytes -= size
                    return message
                if self._reader_error is not None:
                    raise self._reader_error
                waiter = loop.create_future()
                self._read_waiter = waiter
            try:
                await waiter
            finally:
                with self._receive_mutex:
                    if self._read_waiter is waiter:
                        self._read_waiter = None

    def diagnostics(self) -> dict[str, Any]:
        snapshot = super().diagnostics()
        with self._receive_mutex:
            snapshot["receive_pending"] = len(self._pending)
            snapshot["receive_pending_bytes"] = self._pending_bytes
        return snapshot

    @staticmethod
    def _decode_pipe_frame(frame: bytes) -> dict[str, Any]:
        header_size = HEADER.size
        if len(frame) < header_size:
            raise IpcError("ipc pipe frame is truncated")
        (length,) = HEADER.unpack(frame[:header_size])
        body = frame[header_size:]
        if length != len(body):
            raise IpcError("ipc pipe frame length mismatch")
        return decode_frame(body)

    async def recv(self) -> dict[str, Any]:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        self._receiving_since = time.monotonic()
        try:
            async with self._receive_lock:
                message = await self._next_message()
            self._last_rx = time.monotonic()
            return message
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

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            with self._receive_mutex:
                self._pending.clear()
                self._pending_bytes = 0
            self._notify_reader()
            self._close_task = asyncio.create_task(self._close_native_pipe())
        # A cancelled caller must not abandon the native handle. Retrying close
        # waits for this same cleanup instead of reporting an unfinished close.
        await asyncio.shield(self._close_task)

    async def _close_native_pipe(self) -> None:
        try:
            # poll() owns a Windows Overlapped read even on an idle pipe.
            # Let its bounded wait collect completion before CloseHandle.
            # The default executor can be occupied by Runtime tools.
            while not self._read_native_lock.acquire(blocking=False):
                await asyncio.sleep(0.005)
            try:
                close = getattr(self._conn, "close", None)
                if callable(close):
                    close()
            finally:
                self._read_native_lock.release()
        finally:
            self._reader_pool.shutdown(wait=False, cancel_futures=True)
            self._writer_pool.shutdown(wait=False, cancel_futures=True)
