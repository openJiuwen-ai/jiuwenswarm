# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound directly inherited Worker stderr without a Front-owned output pipe."""

from __future__ import annotations

import atexit
import datetime
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

STDERR_LOG_ENV = "JIUWENSWARM_WORKER_STDERR_LOG"
_LOG_NAME = re.compile(r"runtime-worker-[0-9a-f]{32}\.(?:stderr|stdout)(?:_\d{8}_\d{6}_\d+)?\.log")
_DUMP_NAME = re.compile(r"supervisor-\d+-\d+\.txt")
_rotation: WorkerLogRotation | None = None


def open_worker_stderr(path: Path) -> BinaryIO:
    """Open an append-only stderr handle that remains append-only in a child.

    Windows does not inherit the CRT's O_APPEND flag through Popen. Request
    FILE_APPEND_DATA without FILE_WRITE_DATA so native and inherited writes
    still append after copy/truncate rotation instead of leaving sparse holes.
    """
    if os.name != "nt":
        return path.open("ab", buffering=0)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    append_data = 0x0004
    read_attributes = 0x0080
    share_read_write = 0x0001 | 0x0002
    open_always = 4
    normal_file = 0x0080
    handle = kernel.CreateFileW(
        str(path), append_data | read_attributes, share_read_write,
        None, open_always, normal_file, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "ab", buffering=0)
    except OSError:
        os.close(descriptor)
        raise


def _active_log_paths(directory: Path) -> set[str] | None:
    """Protect open files, including stderr inherited by SDK/tool children.

    Unknown process access protects old base logs rather than guessing that
    they are retired. Immutable archives can still be pruned in that case.
    """
    import psutil

    active = set()
    root = os.path.normcase(str(directory.resolve()))
    try:
        username = psutil.Process().username()
        deadline = time.monotonic() + 2.0
        for process in psutil.process_iter(["pid", "username"]):
            try:
                if process.info["username"] is None or time.monotonic() >= deadline:
                    return None
                if process.info["username"] != username:
                    continue
                for entry in process.open_files():
                    path = Path(entry.path)
                    if os.path.normcase(str(path.parent)) == root:
                        active.add(os.path.normcase(str(path)))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except (psutil.AccessDenied, OSError, NotImplementedError):
        return None
    return active


def _prune(files: list[tuple[float, int, Path]], *, max_bytes: int, backup_count: int) -> None:
    """Remove oldest closed files until both the byte and count budgets fit."""
    files.sort(key=lambda item: (item[0], item[2].name))
    remaining = len(files)
    total_bytes = sum(item[1] for item in files)
    for modified, size, path in files:
        if remaining <= backup_count and total_bytes <= max_bytes * backup_count:
            break
        # A different Worker/Front may still be creating this file. It also
        # protects the short interval between Popen's file open and child start.
        if modified > time.time() - 60:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        remaining -= 1
        total_bytes -= size


def prune_worker_logs(
    directory: Path, *, max_bytes: int, backup_count: int, protected: Path | None = None,
) -> None:
    """Bound old Worker outputs and Supervisor snapshots in this directory."""
    outputs = []
    dumps = []
    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if _LOG_NAME.fullmatch(path.name):
            kind = outputs
        elif _DUMP_NAME.fullmatch(path.name):
            kind = dumps
        else:
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            kind.append((info.st_mtime, info.st_size, path))
    if len(outputs) > backup_count or sum(item[1] for item in outputs) > max_bytes * backup_count:
        active = _active_log_paths(directory)
        outputs = [
            item for item in outputs
            if item[2] != protected
            and (
                (".stderr_" in item[2].name or ".stdout_" in item[2].name)
                or (active is not None and os.path.normcase(str(item[2].resolve())) not in active)
            )
        ]
        _prune(outputs, max_bytes=max_bytes, backup_count=backup_count)
    _prune(dumps, max_bytes=max_bytes, backup_count=backup_count)


class WorkerLogRotation:
    """Copy/truncate stderr in the Worker, including while Front is detached."""

    def __init__(
        self, path: Path, *, max_bytes: int, backup_count: int,
        interval: float = 5.0, retention_interval: float = 60.0,
    ) -> None:
        self.path = path
        if max_bytes <= 0 or backup_count <= 0 or interval <= 0 or retention_interval <= 0:
            raise ValueError("Worker log rotation limits and intervals must be positive")
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._interval = interval
        self._retention_interval = retention_interval
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="worker-stderr-rotation", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Do not let a blocked disk extend Worker shutdown."""
        self._stopped.set()
        self._thread.join(timeout=0.2)

    def rotate(self) -> None:
        """Archive bounded chunks, then truncate the same inherited file."""
        if self.path.stat().st_size < self.max_bytes:
            return
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        with self.path.open("r+b", buffering=0) as source:
            size = os.fstat(source.fileno()).st_size
            remaining = min(size, self.max_bytes * self.backup_count)
            source.seek(size - remaining)
            while remaining:
                archive = self.path.with_name(f"{self.path.stem}_{stamp}_{time.time_ns()}{self.path.suffix}")
                temporary = archive.with_suffix(".tmp")
                chunk_bytes = min(remaining, self.max_bytes)
                try:
                    with temporary.open("xb") as target:
                        while chunk_bytes:
                            chunk = source.read(min(chunk_bytes, 64 * 1024))
                            if not chunk:
                                raise OSError("Worker stderr changed while archiving")
                            target.write(chunk)
                            remaining -= len(chunk)
                            chunk_bytes -= len(chunk)
                    temporary.replace(archive)
                    self._prune_archives()
                finally:
                    temporary.unlink(missing_ok=True)
            source.truncate(0)

    def _prune_archives(self) -> None:
        """Keep the current Worker's immutable archives within their budget."""
        # The current file's archives are immutable and safe to prune without
        # process inspection. This budget applies immediately, even under bursts.
        archives = sorted(
            path for path in self.path.parent.glob(f"{self.path.stem}_*{self.path.suffix}")
            if _LOG_NAME.fullmatch(path.name) and not path.is_symlink()
        )
        for old in archives[:-self.backup_count]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                # A reader may deny deletion on Windows. Keep rotating
                # the active file and retry archive retention next time.
                continue

    def _run(self) -> None:
        last_retention = float("-inf")
        last_warning = float("-inf")
        while not self._stopped.is_set():
            try:
                self.rotate()
                now = time.monotonic()
                if now - last_retention >= self._retention_interval:
                    last_retention = now
                    prune_worker_logs(
                        self.path.parent, max_bytes=self.max_bytes,
                        backup_count=self.backup_count, protected=self.path,
                    )
            except Exception:  # noqa: BLE001 - logging maintenance must not stop the Worker
                now = time.monotonic()
                if now - last_warning >= 60:
                    last_warning = now
                    logger.warning("[Worker] output log maintenance failed", exc_info=True)
            if self._stopped.wait(self._interval):
                break


def start_worker_log_rotation(
    *, max_bytes: int | None = None, backup_count: int | None = None,
    interval: float = 5.0, retention_interval: float = 60.0,
) -> WorkerLogRotation | None:
    """Start retention only for the actual stderr file supplied by the parent."""
    global _rotation
    if _rotation is not None:
        return _rotation
    value = os.environ.get(STDERR_LOG_ENV, "")
    if not value or sys.stderr is None:
        return None
    path = Path(value)
    try:
        if not _LOG_NAME.fullmatch(path.name) or not os.path.samestat(path.stat(), os.fstat(sys.stderr.fileno())):
            return None
    except (AttributeError, OSError, ValueError):
        return None
    from jiuwenswarm.common.utils import _LOG_FILE_BACKUP_COUNT, _LOG_FILE_MAX_BYTES

    try:
        rotation = WorkerLogRotation(
            path, max_bytes=_LOG_FILE_MAX_BYTES if max_bytes is None else max_bytes,
            backup_count=_LOG_FILE_BACKUP_COUNT if backup_count is None else backup_count,
            interval=interval, retention_interval=retention_interval,
        )
        rotation.start()
    except (OSError, RuntimeError, ValueError):
        logger.warning("[Worker] output log rotation could not start", exc_info=True)
        return None
    atexit.register(rotation.stop)
    _rotation = rotation
    return rotation


def cleanup_worker_logs(directory: Path) -> None:
    """Apply retention on launch too, even if a previous Worker failed to boot."""
    from jiuwenswarm.common.utils import _LOG_FILE_BACKUP_COUNT, _LOG_FILE_MAX_BYTES

    try:
        prune_worker_logs(directory, max_bytes=_LOG_FILE_MAX_BYTES, backup_count=_LOG_FILE_BACKUP_COUNT)
    except Exception:  # noqa: BLE001 - cleanup must not prevent a replacement Worker
        logger.warning("[Supervisor] old Worker log cleanup failed", exc_info=True)
