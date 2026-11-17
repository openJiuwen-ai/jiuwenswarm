"""Supervisor output files opened by the host before sandbox privilege drop."""
from __future__ import annotations

import errno
import os
import stat
import tempfile
from contextlib import ExitStack
from typing import BinaryIO


class _StartupLog:
    """Read this launch's stderr without moving the child's shared file offset."""

    def __init__(self, stream: BinaryIO) -> None:
        self._fd = stream.fileno()
        self._start = os.fstat(self._fd).st_size
        self._position = 0

    def seek(self, position: int) -> None:
        self._position = position

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = max(0, os.fstat(self._fd).st_size - self._start - self._position)
        data = os.pread(self._fd, size, self._start + self._position)
        self._position += len(data)
        return data


class RuntimeOutput:
    """Append to optional host files; keep unspecified streams in startup capture.

    Parents must exist. New files are private, final symlinks and non-regular
    files are rejected, and opening a second stream never leaks the first FD.
    The daemon inherits the files; closing the parent's copies is independent.
    """

    def __init__(self, stdout_path: str | None, stderr_path: str | None) -> None:
        self._files = ExitStack()
        try:
            capture = self._files.enter_context(tempfile.TemporaryFile())
            self.stdout = self._open(stdout_path) if stdout_path else capture
            self.stderr = self._open(stderr_path) if stderr_path else capture
            self.startup_log = _StartupLog(self.stderr)
        except BaseException:
            self.close()
            raise

    def _open(self, path: str) -> BinaryIO:
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "runtime output must be a regular file", path)
            stream = os.fdopen(fd, "a+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise
        return self._files.enter_context(stream)

    def close(self) -> None:
        self._files.close()

    def __enter__(self) -> RuntimeOutput:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
