"""Supervisor output files opened by the host before sandbox privilege drop."""
from __future__ import annotations

import errno
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO


def _log_root() -> Path:
    configured = os.environ.get("JIUWENBOX_RUNTIME_LOG_DIR")
    if configured:
        return Path(configured)
    if os.environ.get("JIUWENBOX_RUNTIME_PROFILE") == "agentos":
        return Path("/var/log/agentos/adx_sessions/runtime")
    return Path("/opt/adx/logs/runtime")


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


@contextmanager
def _open_stderr_reader(writer: BinaryIO) -> Iterator[BinaryIO]:
    with open(f"/proc/self/fd/{writer.fileno()}", "rb", buffering=0) as reader:
        yield reader


class RuntimeOutput:
    """Append to optional host files; keep unspecified streams in startup capture.

    Redirected streams are write-only for the daemon. The host reads the startup
    marker through a separate descriptor on the same stderr file.
    """

    def __init__(self, stdout_path: str | None, stderr_path: str | None) -> None:
        self._files = ExitStack()
        try:
            capture = self._files.enter_context(tempfile.TemporaryFile())
            self.stdout = self._open(stdout_path) if stdout_path else capture
            self.stderr = self._open(stderr_path) if stderr_path else capture
            stderr_reader = capture
            if stderr_path:
                stderr_reader = self._files.enter_context(_open_stderr_reader(self.stderr))
            self.startup_log = _StartupLog(stderr_reader)
        except BaseException:
            self.close()
            raise

    def _open(self, path: str) -> BinaryIO:
        target = Path(path)
        root = _log_root()
        if not root.is_absolute() or target.parent != root:
            raise PermissionError("runtime output must be directly under JIUWENBOX_RUNTIME_LOG_DIR")
        directory = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, mode=0o700,
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            metadata = os.fstat(directory)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise PermissionError("runtime log directory must be owned by the management service")
            fd = os.open(target.name, flags, 0o600, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.EINVAL, "runtime output must be a regular file", path)
            if metadata.st_nlink != 1 or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise PermissionError("runtime output must be a service-owned file without hard links")
            stream = os.fdopen(fd, "ab", buffering=0)
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
