# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound directly inherited Worker stderr without a Front-owned output pipe."""

from __future__ import annotations

import atexit
import codecs
import datetime
import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from functools import cache
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

STDERR_LOG_ENV = "JIUWENSWARM_WORKER_STDERR_LOG"
_LOG_PREFIX = r"runtime-worker-[0-9a-f]{32}(?:-(?P<owner>[0-9a-f]{32}))?"
_LOG_NAME = re.compile(_LOG_PREFIX + r"\.(?:stderr|stdout)(?:_\d{8}_\d{6}_\d+)?\.log")
_TEMP_NAME = re.compile(_LOG_PREFIX + r"\.(?:stderr|stdout)_\d{8}_\d{6}_\d+\.tmp")
_DUMP_NAME = re.compile(r"supervisor-\d+-\d+\.txt")
_rotation: WorkerLogRotation | None = None


def _darwin_boot_id() -> str:
    """Read the kernel's boot-session UUID without starting a subprocess."""
    import ctypes

    sysctl = ctypes.CDLL(None, use_errno=True).sysctlbyname
    sysctl.argtypes = (
        ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p, ctypes.c_size_t,
    )
    sysctl.restype = ctypes.c_int
    value = ctypes.create_string_buffer(37)
    size = ctypes.c_size_t(len(value))
    if sysctl(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read boot-session UUID")
    return uuid.UUID(value.value.decode("ascii")).hex


def _local_log_owner() -> str | None:
    """Identify the local process view, never a configurable hostname.

    A complete local process scan says nothing about another host or PID
    namespace sharing this directory. Only matching ownership permits that
    scan to establish closure. Missing evidence must keep mutable files.
    """
    if os.name == "nt":
        return None
    try:
        if sys.platform.startswith("linux"):
            boot = uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()).hex
            pid_namespace = Path("/proc/self/ns/pid").stat()
            user_namespace = Path("/proc/self/ns/user").stat()
            # Field 22 follows the parenthesized command and fields 3..21.
            start = int(Path("/proc/1/stat").read_text(encoding="utf-8").rpartition(")")[2].split()[19])
            identity = (
                f"{boot}:{pid_namespace.st_dev}:{pid_namespace.st_ino}:"
                f"{user_namespace.st_dev}:{user_namespace.st_ino}:{start}:{os.geteuid()}"
            )
        elif sys.platform == "darwin":
            identity = f"{_darwin_boot_id()}:{os.geteuid()}"
        else:
            return None
    except (AttributeError, IndexError, OSError, ValueError):
        logger.warning("[Worker] output log ownership unavailable; keeping mutable logs", exc_info=True)
        return None
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:32]


def worker_stderr_name(worker_id: str) -> str:
    """Tag POSIX outputs with the process view that can prove their closure."""
    owner = _local_log_owner()
    suffix = f"-{owner}" if owner is not None else ""
    return f"runtime-worker-{worker_id}{suffix}.stderr.log"


@cache
def _windows_file_api() -> Any:
    """Resolve the Windows file API without loading it on POSIX."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _windows_log_is_closed(path: Path) -> bool:
    """Probe delete sharing without deleting or renaming the file.

    Both legacy CRT stderr and our append-only handles deny delete sharing.
    An inherited handle therefore protects the file even after its Worker
    exits, without inspecting unrelated or protected system processes.
    """
    import ctypes

    kernel = _windows_file_api()
    delete_access = 0x00010000
    share_read_write_delete = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    normal_file = 0x0080
    handle = kernel.CreateFileW(
        str(path), delete_access, share_read_write_delete,
        None, open_existing, normal_file, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


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

    kernel = _windows_file_api()
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

    Numeric UIDs avoid username lookups for unrelated processes. An incomplete
    scan still protects base logs; a partial active set cannot prove closure.
    """
    import psutil

    active = set()
    root = os.path.normcase(str(directory.resolve()))
    try:
        uid = os.geteuid()
        deadline = time.monotonic() + 2.0
        for process in psutil.process_iter(["pid", "uids"]):
            try:
                if time.monotonic() >= deadline:
                    logger.warning("[Worker] output log cleanup deferred: process scan timed out")
                    return None
                uids = process.info["uids"]
                if uids is None:
                    # process_iter may cache an unavailable attribute. Recheck
                    # it so an exited process does not poison the whole scan.
                    uids = process.uids()
                if uid not in uids:
                    continue
                for entry in process.open_files():
                    path = Path(entry.path)
                    if os.path.normcase(str(path.parent)) == root:
                        active.add(os.path.normcase(str(path)))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                logger.warning(
                    "[Worker] output log cleanup deferred: process access denied (pid=%s)", process.pid,
                )
                return None
    except (psutil.AccessDenied, OSError, NotImplementedError) as error:
        logger.warning("[Worker] output log cleanup deferred: process scan unavailable (%s)", type(error).__name__)
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
    temporary_outputs = []
    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if _LOG_NAME.fullmatch(path.name):
            kind = outputs
        elif _TEMP_NAME.fullmatch(path.name):
            kind = temporary_outputs
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
    prune_outputs = len(outputs) > backup_count or sum(item[1] for item in outputs) > max_bytes * backup_count
    if prune_outputs or temporary_outputs:
        active = _active_log_paths(directory) if os.name != "nt" else None
        owner = _local_log_owner() if os.name != "nt" else None
        closed = []
        abandoned = []
        for item in outputs + temporary_outputs:
            path = item[2]
            if path == protected:
                continue
            eligible = abandoned if path.suffix == ".tmp" else closed
            if path.suffix == ".log" and (".stderr_" in path.name or ".stdout_" in path.name):
                eligible.append(item)
            elif os.name == "nt":
                if _windows_log_is_closed(path):
                    eligible.append(item)
            elif active is not None and owner is not None:
                match = (_TEMP_NAME if path.suffix == ".tmp" else _LOG_NAME).fullmatch(path.name)
                if match.group("owner") == owner and os.path.normcase(str(path.resolve())) not in active:
                    eligible.append(item)
        if prune_outputs:
            _prune(closed, max_bytes=max_bytes, backup_count=backup_count)
        # An interrupted copy is disposable; keep its complete source log.
        _prune(abandoned, max_bytes=1, backup_count=0)
    _prune(dumps, max_bytes=max_bytes, backup_count=backup_count)


class WorkerLogRotation:
    """Copy/truncate stderr in the Worker, including while Front is detached."""

    def __init__(
        self, path: Path, *, max_bytes: int, backup_count: int,
        interval: float = 5.0, retention_interval: float = 60.0,
    ) -> None:
        self.path = path
        if max_bytes < 4:
            raise ValueError("Worker log size must fit a UTF-8 character")
        if backup_count <= 0 or interval <= 0 or retention_interval <= 0:
            raise ValueError("Worker log rotation limits and intervals must be positive")
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._interval = interval
        self._retention_interval = retention_interval
        self._last_archive_ns = 0
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
            # Each intermediate UTF-8 boundary can move back by three bytes.
            # Reserve that space so even an oversized file fits this many archives.
            budget = self.max_bytes * self.backup_count - 3 * (self.backup_count - 1)
            remaining = min(size, budget)
            source.seek(size - remaining)
            if size > remaining:
                for _ in range(3):
                    first = source.read(1)
                    if not first or first[0] & 0xC0 != 0x80:
                        if first:
                            source.seek(-1, os.SEEK_CUR)
                        break
                    remaining -= 1
            while remaining:
                # Windows wall-clock reads may repeat within one rotation.
                # Keep the suffix strictly increasing so replace cannot overwrite
                # an earlier chunk emitted in the same clock tick.
                self._last_archive_ns = max(time.time_ns(), self._last_archive_ns + 1)
                archive = self.path.with_name(f"{self.path.stem}_{stamp}_{self._last_archive_ns}{self.path.suffix}")
                temporary = archive.with_suffix(".tmp")
                chunk_bytes = min(remaining, self.max_bytes)
                tail = b""
                try:
                    with temporary.open("xb") as target:
                        while chunk_bytes:
                            chunk = source.read(min(chunk_bytes, 64 * 1024))
                            if not chunk:
                                raise OSError("Worker stderr changed while archiving")
                            target.write(chunk)
                            tail = (tail + chunk)[-4:]
                            remaining -= len(chunk)
                            chunk_bytes -= len(chunk)
                        if remaining:
                            decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
                            decoder.decode(tail, final=False)
                            pending = len(decoder.getstate()[0])
                            if pending:
                                target.seek(-pending, os.SEEK_CUR)
                                target.truncate()
                                source.seek(-pending, os.SEEK_CUR)
                                remaining += pending
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
    if not value:
        return None
    if sys.stderr is None:
        logger.warning("[Worker] output log rotation could not start: stderr is unavailable")
        return None
    path = Path(value)
    try:
        if not _LOG_NAME.fullmatch(path.name):
            logger.warning("[Worker] output log rotation could not start: invalid stderr log name")
            return None
        if not os.path.samestat(path.stat(), os.fstat(sys.stderr.fileno())):
            logger.warning("[Worker] output log rotation could not start: stderr file identity mismatch")
            return None
    except (AttributeError, OSError, ValueError):
        logger.warning("[Worker] output log rotation could not start: stderr validation failed", exc_info=True)
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


