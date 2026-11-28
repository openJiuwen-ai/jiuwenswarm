# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instance lock and PID file management.

This module provides:
- InstanceLock: Cross-platform file lock for instance startup concurrency
- PID management: write/read/delete PID files, process alive detection
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from jiuwenswarm.instance_manager.config import (
    InstanceConfig,
    PID_FILENAME,
    _get_system_executable,
)

logger = logging.getLogger(__name__)

# Lock filename for instance startup concurrency control
LOCK_FILENAME = ".instance.lock"
# Stale lock timeout in seconds (locks older than this are considered stale)
STALE_LOCK_TIMEOUT = 30.0

# The per-workspace Gateway singleton lives in gateway.gateway_lock.
# Do not import it here: instance_manager/__init__.py loads this module.


class InstanceLock:
    """Cross-platform file lock for instance startup concurrency control.

    Prevents race conditions when multiple processes attempt to start
    the same instance simultaneously. Uses platform-specific locking:
    - Unix: fcntl.flock (POSIX advisory lock)
    - Windows: exclusive file creation with timestamp-based stale detection

    Usage:
        lock = InstanceLock(config)
        if not lock.acquire(timeout=5.0):
            print("Instance startup in progress")
            return
        try:
            # Start instance...
            write_pid_file(config, os.getpid())
        finally:
            lock.release()

    Note:
        The lock is advisory on Unix and uses file existence on Windows.
        Always acquire before PID file operations to ensure consistency.
    """

    def __init__(self, config: InstanceConfig):
        """Initialize lock for given instance.

        Args:
            config: InstanceConfig to lock
        """
        self.config = config
        self.lock_path = config.workspace / LOCK_FILENAME
        self._lock_file: Optional[Any] = None

    def acquire(self, timeout: float = 5.0) -> bool:
        """Acquire exclusive lock for instance startup.

        Args:
            timeout: Max seconds to wait for lock acquisition

        Returns:
            True if lock acquired, False if timeout/in use
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        system = platform.system().lower()

        if system == "windows":
            return self._acquire_windows(timeout)
        else:
            return self._acquire_unix(timeout)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file is not None:
            try:
                system = platform.system().lower()
                if system != "windows":
                    # Unix: release flock
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception as exc:
                logger.debug("Lock release error (ignored): %s", exc)
            finally:
                self._lock_file = None

            # On Windows, also remove the lock file
            if system == "windows":
                try:
                    if self.lock_path.exists():
                        self.lock_path.unlink()
                except OSError as exc:
                    logger.debug("Lock file removal error (ignored): %s", exc)

    def _acquire_unix(self, timeout: float) -> bool:
        """Unix implementation using fcntl.flock."""
        import fcntl

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self._lock_file = open(self.lock_path, 'w')
                fcntl.flock(
                    self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                # Write lock info for debugging
                self._lock_file.write(f"{os.getpid()}\n{time.time()}\n")
                self._lock_file.flush()
                return True
            except (IOError, OSError):
                if self._lock_file is not None:
                    try:
                        self._lock_file.close()
                    except Exception as exc:
                        logger.debug(
                            "Lock file close error during retry (ignored): %s",
                            exc
                        )
                    self._lock_file = None
                time.sleep(0.1)

        return False

    def _acquire_windows(self, timeout: float) -> bool:
        """Windows implementation using exclusive file creation.

        Since Windows doesn't have fcntl, we use exclusive file creation
        combined with timestamp-based stale lock detection.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                # Try exclusive creation (fails if file exists)
                self._lock_file = open(self.lock_path, 'x', encoding='utf-8')
                # Write lock info
                self._lock_file.write(f"{os.getpid()}\n{time.time()}\n")
                self._lock_file.flush()
                return True
            except FileExistsError:
                # Lock file exists - check if stale
                if self._is_stale_lock():
                    self._remove_stale_lock()
                    continue
                time.sleep(0.1)
            except OSError:
                # Other OS error (permissions, etc.)
                time.sleep(0.1)

        return False

    def _is_stale_lock(self) -> bool:
        """Check if existing lock file is stale (older than STALE_LOCK_TIMEOUT)."""
        try:
            stat = self.lock_path.stat()
            age = time.time() - stat.st_mtime
            return age > STALE_LOCK_TIMEOUT
        except OSError:
            return False

    def _remove_stale_lock(self) -> None:
        """Remove stale lock file."""
        try:
            self.lock_path.unlink()
            logger.info("Removed stale lock file: %s", self.lock_path)
        except OSError:
            pass

    def __enter__(self) -> "InstanceLock":
        """Context manager entry."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.release()


def write_pid_file(
    config: InstanceConfig,
    pid: int,
    started_at: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Write PID file for a running instance.

    File format (JSON):
    {
        "pid": <process_id>,
        "started_at": <timestamp>,
        "name": <instance_name>
    }

    The write is atomic: the record goes to a unique temp file beside the target
    and ``os.replace`` swaps it in, so a reader always sees a complete file. The
    replace is retried for a short window while another process still holds the
    old file open on Windows.

    Args:
        config: InstanceConfig for the instance
        pid: Process ID to write
        started_at: Startup timestamp, defaults to current time
    """
    pid_path = config.get_pid_file_path()
    if started_at is None:
        started_at = time.time()

    data: Dict[str, Any] = {
        "pid": pid,
        "started_at": started_at,
        "name": config.name,
    }
    if metadata:
        data.update(metadata)

    # Atomic write: unique temp file + replace. A fixed .tmp name let concurrent
    # writers delete each other's file, and unlinking the target first left a
    # window where readers saw no PID file at all.
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = pid_path.with_name(f".{pid_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _replace_with_retry(temp_path, pid_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    logger.info(
        "Wrote PID file for instance '%s': pid=%d, path=%s",
        config.name, pid, pid_path
    )


def _replace_with_retry(source: Path, target: Path, max_attempts: int = 10) -> None:
    """Retry the replace while another process or an indexer holds the PID file."""
    for attempt in range(max(1, max_attempts)):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == max(1, max_attempts) - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_pid_file(config: InstanceConfig) -> Optional[Dict[str, Any]]:
    """Read PID file for an instance.

    Args:
        config: InstanceConfig for the instance

    Returns:
        Dict with pid, started_at, name if file exists and valid, None otherwise
    """
    pid_path = config.get_pid_file_path()
    if not pid_path.exists():
        return None

    try:
        data = json.loads(pid_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, IOError):
        return None


def delete_pid_file(
    config: InstanceConfig, expected_data: Optional[Dict[str, Any]] = None
) -> bool:
    """Delete PID file for an instance.

    Args:
        config: InstanceConfig for the instance

    Returns:
        True if file was deleted, False if it didn't exist
    """
    pid_path = config.get_pid_file_path()
    if not pid_path.exists():
        return False
    if expected_data is not None and read_pid_file(config) != expected_data:
        logger.warning(
            "PID file for instance '%s' changed during stop; retaining newer record",
            config.name,
        )
        return False
    pid_path.unlink()
    logger.info(
        "Deleted PID file for instance '%s': %s", config.name, pid_path
    )
    return True


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is alive.

    Args:
        pid: Process ID to check

    Returns:
        True if process is running, False otherwise
    """
    if pid <= 0:
        return False

    system = platform.system().lower()

    if system == "windows":
        try:
            result = subprocess.run(
                [
                    _get_system_executable("tasklist"),
                    "/FI", f"PID eq {pid}", "/NH"
                ],
                capture_output=True,
                text=True,
                # Pin the codec. With ``text=True`` alone the child's output is
                # decoded with ``io.text_encoding(None)``: the locale codec until
                # Python runs in UTF-8 mode, ``utf-8`` after. ``uv run`` sets
                # PYTHONUTF8=1, while ``tasklist`` still emits ANSI/OEM bytes, so
                # the decode raised UnicodeDecodeError inside subprocess' reader
                # thread -- the exception never reached this caller, stdout came
                # back empty, and a live process was reported as dead. Only ASCII
                # tokens are parsed below, so a tolerant handler is safe.
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            # tasklist returns "INFO: No tasks are running..." if not found
            return str(pid) in result.stdout and "INFO:" not in result.stdout
        except Exception:
            return False
    else:
        # Unix: send signal 0 to check existence
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def check_instance_running(workspace: Path) -> bool:
    """Check if instance is running via PID file (legacy interface).

    Args:
        workspace: Instance workspace path

    Returns:
        True if instance is running, False otherwise
    """
    pid_file = workspace / PID_FILENAME
    if not pid_file.exists():
        return False

    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = data.get("pid", 0)
        if not isinstance(pid, int) or pid <= 0:
            return False
        return is_process_alive(pid)
    except (json.JSONDecodeError, IOError):
        return False
