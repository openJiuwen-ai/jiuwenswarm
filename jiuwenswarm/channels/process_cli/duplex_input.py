# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancellable, bounded UTF-8 line input for one duplex command process.

The reader exclusively borrows stdin: it neither changes descriptor modes nor
starts a worker thread. Windows pipes use PeekNamedPipe before reading available
bytes; POSIX pipes use zero-timeout select. A command can therefore finish while
its parent keeps stdin open, without blocking asyncio's executor shutdown.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import select
import stat
import sys
from types import TracebackType
from typing import BinaryIO, NoReturn, TextIO


MAX_DUPLEX_LINE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PIPE_EOF_ERRORS = (109, 232, 233)


class DuplexInputError(ValueError):
    """Safe input-framing diagnostic, without request contents or file paths."""

    code = "INVALID_INPUT"


class _MemorySource:
    """Adapt in-memory test streams without exceeding each requested byte count."""

    def __init__(self, stream: io.BytesIO | io.StringIO) -> None:
        self._stream = stream
        self._pending = b""

    def read_available(self, size: int) -> bytes:
        if not self._pending:
            # A Unicode scalar can require four UTF-8 bytes. Keep even this
            # test adapter's temporary encoding buffer bounded by one chunk.
            value = self._stream.read(min(size, _READ_CHUNK_BYTES // 4))
            self._pending = value.encode("utf-8") if isinstance(value, str) else value
        result = self._pending[:size]
        self._pending = self._pending[size:]
        return result

    def discard_buffer(self) -> None:
        self._pending = b""


class _WindowsPipe:
    """Check a synchronously opened Windows pipe before each bounded read."""

    def __init__(self, fd: int) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self._fd = fd
        self._handle = msvcrt.get_osfhandle(fd)
        self._ctypes = ctypes
        self._available_type = wintypes.DWORD
        self._peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        self._peek.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPDWORD,
            wintypes.LPDWORD,
        )
        self._peek.restype = wintypes.BOOL

    def read_available(self, size: int) -> bytes | None:
        available = self._available_type()
        success = self._peek(
            self._handle, None, 0, None, self._ctypes.byref(available), None
        )
        if not success:
            error = self._ctypes.get_last_error()
            if error in _PIPE_EOF_ERRORS:
                return b""
            raise OSError(error, "cannot inspect the input pipe")
        if available.value == 0:
            return None
        return os.read(self._fd, min(size, available.value))


class _DescriptorSource:
    """Borrow a descriptor without changing its blocking or inheritance flags."""

    def __init__(self, stream: BinaryIO | TextIO | int) -> None:
        self._fd = stream if isinstance(stream, int) else stream.fileno()
        self._regular_file = stat.S_ISREG(os.fstat(self._fd).st_mode)
        self._windows_pipe = None
        if os.name == "nt" and not self._regular_file:
            self._windows_pipe = _WindowsPipe(self._fd)

    def read_available(self, size: int) -> bytes | None:
        if self._regular_file:
            return os.read(self._fd, size)
        if self._windows_pipe is not None:
            return self._windows_pipe.read_available(size)
        readable, _, _ = select.select([self._fd], [], [], 0)
        if not readable:
            return None
        return os.read(self._fd, size)


def _validate_limits(max_line_bytes: int, poll_interval: float) -> None:
    if isinstance(max_line_bytes, bool) or not isinstance(max_line_bytes, int):
        raise TypeError("max_line_bytes must be an integer")
    if max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be positive")
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)):
        raise TypeError("poll_interval must be a number")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be finite and positive")


class DuplexLineReader:
    """Read complete JSONL frames without waiting for stdin EOF.

    ``read_line`` returns validated UTF-8 bytes without LF or CRLF, or ``None``
    after clean EOF/close. Empty lines remain ``b''`` for the protocol decoder
    to reject. EOF with an unterminated line is always an input error.

    Only one active ``read_line`` is allowed, and no other component may read
    this stream concurrently. ``close`` wakes a pending read and releases all
    buffered bytes, but never closes the borrowed stream or descriptor. The
    live framing buffer holds at most ``max_line_bytes + 2`` bytes.
    """

    def __init__(
        self,
        stream: BinaryIO | TextIO | int | None = None,
        *,
        max_line_bytes: int = MAX_DUPLEX_LINE_BYTES,
        poll_interval: float = 0.01,
    ) -> None:
        _validate_limits(max_line_bytes, poll_interval)
        source = sys.stdin if stream is None else stream
        self._source: _MemorySource | _DescriptorSource
        try:
            if isinstance(source, (io.BytesIO, io.StringIO)):
                self._source = _MemorySource(source)
            else:
                self._source = _DescriptorSource(source)
        except (AttributeError, OSError, ValueError):
            raise DuplexInputError("duplex input stream is not readable") from None
        self._max_line_bytes = max_line_bytes
        self._poll_interval = poll_interval
        self._buffer = bytearray()
        self._closed = asyncio.Event()
        self._eof = False
        self._reading = False

    @property
    def closed(self) -> bool:
        """Whether the reader has been explicitly closed or rejected input."""

        return self._closed.is_set()

    @property
    def buffered_bytes(self) -> int:
        """Current framing-buffer size; input is never accumulated unboundedly."""

        return len(self._buffer)

    def _discard_buffer(self) -> None:
        self._buffer.clear()
        if isinstance(self._source, _MemorySource):
            self._source.discard_buffer()

    def _fail(self, message: str) -> NoReturn:
        self._discard_buffer()
        self._closed.set()
        raise DuplexInputError(message)

    def _take_line(self) -> bytes | None:
        newline = self._buffer.find(b"\n")
        if newline < 0:
            length = len(self._buffer)
            pending_crlf = length == self._max_line_bytes + 1
            if pending_crlf and self._buffer[-1] == 13:
                return None
            if length > self._max_line_bytes:
                self._fail("duplex input line exceeds the byte limit")
            return None
        content_end = newline
        if content_end > 0 and self._buffer[content_end - 1] == 13:
            content_end -= 1
        if content_end > self._max_line_bytes:
            self._fail("duplex input line exceeds the byte limit")
        line = bytes(self._buffer[:content_end])
        del self._buffer[: newline + 1]
        try:
            line.decode("utf-8")
        except UnicodeError:
            self._fail("duplex input line must be valid UTF-8")
        return line

    async def _wait_for_data(self) -> None:
        try:
            async with asyncio.timeout(self._poll_interval):
                await self._closed.wait()
        except TimeoutError:
            pass

    async def read_line(self) -> bytes | None:
        """Await one bounded line; cancellation leaves no thread or read behind."""

        if self._reading:
            raise RuntimeError("only one duplex input read may be active")
        self._reading = True
        try:
            while not self.closed:
                # Yield even when the parent has queued many ready lines, so
                # control input cannot starve execution or cancellation tasks.
                await asyncio.sleep(0)
                if self.closed:
                    return None
                line = self._take_line()
                if line is not None:
                    return line
                if self._eof:
                    if self._buffer:
                        self._fail("duplex input ended before the line terminator")
                    return None
                remaining = self._max_line_bytes + 2 - len(self._buffer)
                try:
                    chunk = self._source.read_available(
                        min(_READ_CHUNK_BYTES, remaining)
                    )
                except UnicodeError:
                    self._fail("duplex input line must be valid UTF-8")
                except (OSError, ValueError):
                    self._fail("duplex input could not be read")
                if chunk is None:
                    await self._wait_for_data()
                elif chunk:
                    self._buffer.extend(chunk)
                else:
                    self._eof = True
            return None
        finally:
            self._reading = False

    async def read_document(self) -> bytes:
        """Read one bounded document to EOF without blocking signal handling.

        Used only for ``--run-json -``. Unlike JSONL, a final newline is not
        required and embedded newlines count against the document byte limit.
        The caller must not mix document and line reads on the same reader.
        """
        if self._reading:
            raise RuntimeError("only one machine input read may be active")
        self._reading = True
        try:
            while not self.closed:
                await asyncio.sleep(0)
                if self.closed:
                    break
                if len(self._buffer) > self._max_line_bytes:
                    self._fail("run input exceeds the byte limit")
                if self._eof:
                    document = bytes(self._buffer)
                    self._discard_buffer()
                    return document
                remaining = self._max_line_bytes + 1 - len(self._buffer)
                try:
                    chunk = self._source.read_available(
                        min(_READ_CHUNK_BYTES, remaining)
                    )
                except (OSError, ValueError):
                    self._fail("run input could not be read")
                if chunk is None:
                    await self._wait_for_data()
                elif chunk:
                    self._buffer.extend(chunk)
                else:
                    self._eof = True
            self._fail("run input closed before EOF")
        finally:
            self._reading = False

    async def close(self) -> None:
        """Wake a pending read and discard buffered input without closing stdin."""

        self._closed.set()
        self._discard_buffer()

    async def __aenter__(self) -> DuplexLineReader:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


__all__ = ["MAX_DUPLEX_LINE_BYTES", "DuplexInputError", "DuplexLineReader"]
