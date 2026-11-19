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
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

from jiuwenswarm.server.ipc.io_loop import on_loop
from jiuwenswarm.server.ipc.protocol import (
    HEADER,
    IpcError,
    KIND_HEARTBEAT,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
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
# Let an active consumer drain a burst without removing the overload bound.
_RECEIVE_BACKLOG_TIMEOUT = 0.5
# Lost/cancelled requests cannot leave an unbounded correlation table on a long-lived IPC session.
_PROBE_REQUEST_HISTORY = 256


def endpoint_address(path: str) -> str:
    """Return the OS address for a logical socket path."""
    if os.name != "nt":
        return path
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:20]
    return rf"\\.\pipe\jiuwenswarm-{digest}"


@dataclass(frozen=True)
class HeartbeatProbeResult:
    state: str
    received_at: float = 0.0
    frame: dict[str, Any] | None = None


class IpcConnection:
    """One bidirectional IPC session."""

    def __init__(self) -> None:
        self._last_tx = self._last_rx = 0.0
        self._send_seconds = self._recv_seconds = self._write_lock_seconds = 0.0
        self._sending_since = self._receiving_since = 0.0
        self._write_waiters: dict[object, float] = {}
        self._stats_lock = threading.Lock()
        self._recent_requests: OrderedDict[tuple[int, str], float] = OrderedDict()
        self._recent_completions: OrderedDict[tuple[int, str], tuple[float, float]] = OrderedDict()
        self._last_delivered_rx = 0.0
        self._probe_lock = asyncio.Lock()
        self._probe_write: asyncio.Task[None] | None = None
        self._native_probe_write: asyncio.Task[None] | None = None
        self._probe_observer: Callable[[dict[str, Any], float | None, float], None] | None = None
        self._heartbeat_responder: Callable[[dict[str, Any]], None] | None = None

    def set_heartbeat_responder(
        self, responder: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Answer heartbeat pings without the event loop where supported.

        Named pipes use a reader thread; socket connections use their owner
        I/O loop. Neither responder needs the Runtime business loop.
        """
        self._heartbeat_responder = responder

    def _remember_request(self, message: dict[str, Any], issued_at: float | None) -> None:
        if issued_at is None:
            return
        generation, dispatch_id = message.get("generation"), message.get("dispatch_id")
        if type(generation) is int and isinstance(dispatch_id, str) and 0 < len(dispatch_id) <= 128:
            with self._stats_lock:
                self._recent_completions.pop((generation, dispatch_id), None)
                self._recent_requests.setdefault((generation, dispatch_id), issued_at)
                if len(self._recent_requests) > _PROBE_REQUEST_HISTORY:
                    self._recent_requests.popitem(last=False)

    @property
    def last_received_at(self) -> float:
        """Decode time of the frame returned by the most recent successful recv."""
        return self._last_delivered_rx

    def _record_frame(self, message: dict[str, Any]) -> float:
        received_at = self._last_rx = time.monotonic()
        issued_at = None
        if message.get("kind") == KIND_REQUEST_DONE:
            generation, dispatch_id = message.get("generation"), message.get("dispatch_id")
            if type(generation) is int and isinstance(dispatch_id, str):
                with self._stats_lock:
                    issued_at = self._recent_requests.pop((generation, dispatch_id), None)
                    if issued_at is not None:
                        self._recent_completions[(generation, dispatch_id)] = (issued_at, received_at)
                        if len(self._recent_completions) > _PROBE_REQUEST_HISTORY:
                            self._recent_completions.popitem(last=False)
        observer = self._probe_observer
        if observer is not None:
            observer(message, issued_at, received_at)
        return received_at

    def _recent_business_reply(
        self, request_since: float | None, generation: int | None,
    ) -> HeartbeatProbeResult | None:
        if request_since is None or type(generation) is not int:
            return None
        with self._stats_lock:
            for (peer, dispatch_id), (issued_at, received_at) in reversed(self._recent_completions.items()):
                if peer == generation and issued_at >= request_since:
                    # Retain correlation metadata only, never business payloads.
                    return HeartbeatProbeResult("business_reply", received_at, {
                        "kind": KIND_REQUEST_DONE, "generation": peer, "dispatch_id": dispatch_id,
                    })
        return None

    async def probe_heartbeat(
        self, timeout: float, *, correlated: bool,
        request_since: float | None = None, generation: int | None = None, write_grace: float = 0.0,
    ) -> HeartbeatProbeResult:
        """Bound a round trip, allowing one extra interval only for an unfinished write."""
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                await self._probe_lock.acquire()
        except TimeoutError:
            return HeartbeatProbeResult("write_timeout")
        try:
            if time.monotonic() >= deadline:
                return HeartbeatProbeResult("write_timeout")
            return await self._probe_until(deadline, correlated, request_since, generation, write_grace)
        finally:
            self._probe_lock.release()

    async def _probe_until(
        self, deadline: float, correlated: bool, request_since: float | None,
        generation: int | None, write_grace: float,
    ) -> HeartbeatProbeResult:
        """Own the reader observation while writing and confirming this probe."""
        token = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        write_deadline = deadline + max(0.0, write_grace)
        reply = asyncio.Event()
        result: HeartbeatProbeResult | None = None

        def observe(message: dict[str, Any], issued_at: float | None, received_at: float) -> None:
            nonlocal result
            if strong_reply() or not started <= received_at <= write_deadline:
                return
            fresh_request = issued_at is not None and request_since is not None and issued_at >= request_since
            legacy_pong = not correlated and "probe_id" not in message
            if fresh_request and message.get("generation") == generation:
                # Only this connection could have received the freshly issued
                # dispatch ID. Its single matching completion proves both directions.
                state = "business_reply"
            elif message.get("kind") == KIND_HEARTBEAT and (message.get("probe_id") == token or legacy_pong):
                state = "received"
            else:
                return
            result = HeartbeatProbeResult(state, received_at, message)
            loop.call_soon_threadsafe(reply.set)

        def strong_reply() -> bool:
            if result is None or result.frame is None:
                return False
            return result.state == "business_reply" or result.frame.get("probe_id") == token

        def outcome(fallback: str) -> HeartbeatProbeResult:
            if getattr(self, "_closed", False) or getattr(self, "_reader_error", None) is not None:
                return HeartbeatProbeResult("connection_closed")
            if result is not None and result.received_at <= deadline:
                if fallback != "write_timeout" or strong_reply():
                    if result.state == "business_reply" and result.frame is not None:
                        with self._stats_lock:
                            self._recent_completions.pop(
                                (result.frame.get("generation"), result.frame.get("dispatch_id")), None,
                            )
                    return result
            return HeartbeatProbeResult(fallback)

        async def wait_for_write(task: asyncio.Task[None]) -> bool:
            nonlocal deadline
            done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            if not done and deadline < write_deadline and outcome("write_timeout").state == "write_timeout":
                # Retain the same write and nonce. Cancelling a native pipe
                # send here would close a healthy connection before it drains.
                deadline = write_deadline
                done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            return bool(done)

        sent_at = None

        async def send_probe() -> None:
            nonlocal sent_at
            await self.send({"kind": KIND_HEARTBEAT, "probe_id": token})
            sent_at = time.monotonic()

        self._probe_observer = observe
        send = None
        try:
            # The I/O reader may finish a post-resume request before the Front
            # registers this observer. That verified round trip is still fresh.
            cached = self._recent_business_reply(request_since, generation)
            if cached is not None and not strong_reply():
                result = cached
            if strong_reply():
                return outcome("write_timeout")
            # A confirmed business reply can arrive while a native probe write is
            # blocked. Keep at most one such write alive on this physical connection.
            previous = self._probe_write
            if previous is not None and not previous.done():
                if not await wait_for_write(previous) or time.monotonic() >= deadline:
                    return outcome("write_timeout")
            send = asyncio.create_task(send_probe())
            self._probe_write = send
            send.add_done_callback(self._finish_probe_write)
            if not await wait_for_write(send):
                return outcome("write_timeout")
            if send.cancelled() and getattr(self, "_closed", False):
                return HeartbeatProbeResult("connection_closed")
            await send
            # A write and its timeout may wake the loop in the same turn.
            if sent_at is not None and sent_at > deadline:
                deadline = write_deadline
            if result is None:
                try:
                    async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                        await reply.wait()
                except TimeoutError:
                    pass
            return outcome("reply_timeout")
        except ConnectionError:
            return HeartbeatProbeResult("connection_closed")
        except (OSError, RuntimeError):
            if getattr(self, "_closed", False) or getattr(self, "_reader_error", None) is not None:
                return HeartbeatProbeResult("connection_closed")
            return HeartbeatProbeResult("write_failed")
        finally:
            self._probe_observer = None
            # A socket write or a request still waiting for the async lock can
            # be cancelled. Cancelling an in-flight Win32 write closes its pipe.
            keep_native_write = self._native_probe_write is send and strong_reply()
            if send is not None and not send.done() and not keep_native_write:
                send.cancel()

    def _cancel_probe_on_close(self) -> None:
        task = self._probe_write
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.get_loop().call_soon_threadsafe(task.cancel)

    def _finish_probe_write(self, task: asyncio.Task[None]) -> None:
        if self._probe_write is task:
            self._probe_write = None
        if not task.cancelled():
            task.exception()

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
        self._pending: deque[tuple[dict[str, Any], int, float]] = deque()
        self._pending_bytes = 0
        self._pending_ready = asyncio.Event()
        self._pending_space = asyncio.Event()
        self._receive_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._reader_error: Exception | None = None
        self._last_frame_bytes = 0

    async def send(self, message: dict[str, Any]) -> None:
        issued_at = time.monotonic() if message.get("kind") == KIND_REQUEST else None
        await on_loop(self._loop, self._send(message, issued_at))

    async def _send(self, message: dict[str, Any], issued_at: float | None) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        frame = encode_frame(message)
        async with self._locked_write():
            self._writer.write(frame)
            self._remember_request(message, issued_at)
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
                        result, size, received_at = self._pending.popleft()
                        self._pending_bytes -= size
                        self._pending_space.set()
                        self._last_delivered_rx = received_at
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
                received_at = self._record_frame(message)
                responder = self._heartbeat_responder
                if responder is not None and message.get("kind") == KIND_HEARTBEAT:
                    responder(message)
                    continue
                size = self._last_frame_bytes
                if (len(self._pending) >= _STREAM_PENDING_MESSAGES
                        or self._pending_bytes + size > _STREAM_PENDING_BYTES):
                    await self._wait_for_receive_space(size, message)
                if self._closed:
                    return
                self._pending.append((message, size, received_at))
                self._pending_bytes += size
                self._pending_ready.set()
        except Exception as exc:  # noqa: BLE001 - recv owns transport/protocol errors
            self._reader_error = exc
        finally:
            self._pending_ready.set()

    async def _wait_for_receive_space(self, size: int, message: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(_RECEIVE_BACKLOG_TIMEOUT):
                while not self._closed and (
                    len(self._pending) >= _STREAM_PENDING_MESSAGES
                    or self._pending_bytes + size > _STREAM_PENDING_BYTES
                ):
                    self._pending_space.clear()
                    await self._pending_space.wait()
        except asyncio.TimeoutError:
            logger.warning(
                "[IPC] business receive backlog exceeded; transport=stream "
                "pending_messages=%s pending_bytes=%s incoming_bytes=%s incoming_kind=%s "
                "limit_messages=%s limit_bytes=%s",
                len(self._pending), self._pending_bytes, size, message.get("kind"),
                _STREAM_PENDING_MESSAGES, _STREAM_PENDING_BYTES,
            )
            raise IpcError("ipc business receive backlog exceeded") from None

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
        self._cancel_probe_on_close()
        self._pending.clear()
        self._pending_bytes = 0
        self._pending_ready.set()
        self._pending_space.set()
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
        self._receive_space = threading.Condition(self._receive_mutex)
        self._pending: deque[tuple[dict[str, Any], int, float]] = deque()
        self._pending_bytes = 0
        self._reader_started = False
        self._reader_error: Exception | None = None
        self._read_waiter: asyncio.Future[None] | None = None
        self._read_notified = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("ipc connection is closed")
        issued_at = time.monotonic() if message.get("kind") == KIND_REQUEST else None
        frame = encode_frame(message)
        async with self._locked_write():
            task = asyncio.current_task()
            if task is self._probe_write:
                self._native_probe_write = task
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._writer_pool, self._send_bytes_locked, frame, message, issued_at
                )
                self._last_tx = time.monotonic()
            except asyncio.CancelledError:
                # The native write can outlive the cancelled coroutine.
                # Abort it before another send can reuse the same pipe.
                await self.close()
                raise
            finally:
                if self._native_probe_write is task:
                    self._native_probe_write = None

    def _send_bytes_locked(
        self, frame: bytes, message: dict[str, Any] | None = None, issued_at: float | None = None,
    ) -> None:
        with self._send_mutex:
            if self._closed:
                raise ConnectionError("ipc connection is closed")
            if issued_at is not None and message is not None:
                self._remember_request(message, issued_at)
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
                message = self._decode_pipe_frame(frame)
                received_at = self._record_frame(message)
                responder = self._heartbeat_responder
                if responder is not None and message.get("kind") == KIND_HEARTBEAT:
                    responder(message)
                    continue
                with self._receive_space:
                    available = self._receive_space.wait_for(
                        lambda: self._closed or (
                            len(self._pending) < _PIPE_PENDING_MESSAGES
                            and self._pending_bytes + len(frame) <= _PIPE_PENDING_BYTES
                        ),
                        timeout=_RECEIVE_BACKLOG_TIMEOUT,
                    )
                    if self._closed:
                        return
                    if not available:
                        logger.warning(
                            "[IPC] business receive backlog exceeded; transport=pipe "
                            "pending_messages=%s pending_bytes=%s incoming_bytes=%s incoming_kind=%s "
                            "limit_messages=%s limit_bytes=%s",
                            len(self._pending), self._pending_bytes, len(frame), message.get("kind"),
                            _PIPE_PENDING_MESSAGES, _PIPE_PENDING_BYTES,
                        )
                        raise IpcError("ipc business receive backlog exceeded")
                    self._pending.append((message, len(frame), received_at))
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
            with self._receive_space:
                if self._closed:
                    raise ConnectionError("ipc connection is closed")
                if self._pending:
                    message, size, received_at = self._pending.popleft()
                    self._pending_bytes -= size
                    self._receive_space.notify()
                    self._last_delivered_rx = received_at
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
                return await self._next_message()
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
            self._cancel_probe_on_close()
            with self._receive_space:
                self._pending.clear()
                self._pending_bytes = 0
                self._receive_space.notify_all()
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
