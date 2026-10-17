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
from pathlib import Path
from typing import Any, Dict, Optional

import portalocker

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

# Lock filename for per-workspace Gateway singleton (held for entire lifetime)
GATEWAY_LOCK_FILENAME = ".gateway.lock"
# Max seconds to wait for a (possibly shutting-down) Gateway to release the lock
GATEWAY_LOCK_ACQUIRE_TIMEOUT = 30.0


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


class GatewayLock:
    """Per-workspace singleton lock for the Gateway process.

    Prevents two Gateway processes from serving the same workspace. A second
    Gateway implies a second independent CronSchedulerService over the same
    ``cron_jobs.json``, which is the root cause of duplicate cron executions
    (course: two instances, two schedulers, one shared store).

    Mutual exclusion is enforced by a persistent **OS-level file lock**
    (portalocker: fcntl.flock on POSIX, LockFileEx on Windows) held for the
    entire Gateway lifetime, so a crashed holder releases the lock
    automatically and a stale lock never needs to be unlinked:

    - the lock file is created once and never removed (no unlink at all, so a
      killer TOCTOU race where process B unlinks the fresh lock just created
      by process A is impossible);
    - the PID stored inside the file is only informational (diagnostics,
      ``find_holder`` preflight); authority comes from the OS lock.

    Usage:
        lock = GatewayLock(get_user_workspace_dir())
        if not lock.acquire():
            logger.error("Another Gateway is already running")
            raise SystemExit(1)
        try:
            asyncio.run(main())
        finally:
            lock.release()
    """

    def __init__(self, workspace: Path) -> None:
        """Initialize lock for given workspace root.

        Args:
            workspace: Workspace root (e.g. ``~/.jiuwenswarm``). The lock file
                lives inside it, so different workspaces never contend.

        Design (mirrors CronJobStore): the OS lock lives on a companion file
        ``.gateway.lock.lock`` (Windows LockFileEx is a *mandatory* lock, so
        the locked file cannot be read by other handles), while the holder
        metadata (pid/workspace) lives in ``.gateway.lock`` which is never
        locked and therefore always readable for preflight checks.
        """
        self.lock_path = workspace / GATEWAY_LOCK_FILENAME
        self._os_lock_path = workspace / (GATEWAY_LOCK_FILENAME + ".lock")
        self._lock: Optional[portalocker.Lock] = None
        self._acquired = False

    def acquire(self, timeout: float = GATEWAY_LOCK_ACQUIRE_TIMEOUT) -> bool:
        """Acquire exclusive lock for this workspace.

        Args:
            timeout: Max seconds to wait for a currently-live Gateway to exit
                (e.g. during an upgrade restart). A stale lock (crashed
                holder) releases the OS lock automatically and is taken over
                immediately without any file deletion.

        Returns:
            True if acquired, False otherwise.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)

        while True:
            lock = portalocker.Lock(
                str(self._os_lock_path),
                mode="a+",
                timeout=None,  # fail_when_locked=True below → a single attempt
                check_interval=0.2,
                fail_when_locked=True,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            )
            try:
                fh = lock.acquire()
            except portalocker.exceptions.AlreadyLocked:
                holder = self._read_holder()
                pid = int(holder.get("pid", 0) or 0) if holder else 0
                if pid == os.getpid():
                    # Self-owned: an os.execv restart reuses the same PID while
                    # the previous image's lock may still be held. We already
                    # own the workspace — treat as acquired.
                    logger.info(
                        "Gateway lock already held by this process, path=%s",
                        self.lock_path,
                    )
                    self._acquired = True
                    return True
                if time.monotonic() >= deadline:
                    logger.error(
                        "Another Gateway is running (pid=%d, workspace=%s); "
                        "refusing duplicate instance: %s",
                        pid,
                        holder.get("workspace", "?") if holder else "?",
                        self.lock_path,
                    )
                    return False
                time.sleep(0.2)
                continue
            except (portalocker.exceptions.LockException, OSError):
                if time.monotonic() >= deadline:
                    logger.error(
                        "Failed to acquire Gateway lock within %.1fs: %s",
                        timeout,
                        self.lock_path,
                    )
                    return False
                time.sleep(0.2)
                continue

            # OS lock acquired: overwrite the holder info, then keep the lock
            # object alive for the entire Gateway lifetime. The metadata file
            # (.gateway.lock) is never OS-locked, so it stays readable.
            try:
                fh.seek(0)  # touch/keep companion file content sane
                self.lock_path.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "started_at": time.time(),
                            "workspace": str(self.lock_path.parent),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                lock.release()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.2)
                continue

            self._lock = lock
            self._acquired = True
            logger.info(
                "Gateway lock acquired, pid=%d, path=%s",
                os.getpid(),
                self.lock_path,
            )
            return True

    def release(self) -> None:
        """Release the lock if we own it."""
        try:
            if self._lock is not None:
                # Clear the holder marker so preflight checks see a free lock,
                # then drop the OS lock (files are kept, never unlinked).
                try:
                    self.lock_path.write_text(
                        json.dumps(
                            {
                                "pid": 0,
                                "started_at": 0,
                                "workspace": str(self.lock_path.parent),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                    logger.debug(
                        "Gateway lock marker clear error (ignored): %s", exc
                    )
                try:
                    self._lock.release()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Gateway lock release error (ignored): %s", exc)
                self._lock = None
        finally:
            self._acquired = False

    @staticmethod
    def find_holder(workspace: Path) -> Optional[Dict[str, Any]]:
        """Return lock metadata if a live Gateway owns this workspace, else None.

        Used by (non-authoritative) preflight checks such as the desktop
        launcher: a result here means another Gateway holds the workspace.

        The metadata file (.gateway.lock) persists after a crash. A stale PID
        that happens to be reused by an unrelated process would cause a false
        positive ("Gateway still running"). To guard against this, after
        confirming the metadata PID is alive we also probe the companion OS
        lock (.gateway.lock.lock): if we can acquire it, the Gateway has
        crashed and released the OS lock, so we return None.
        """
        lock_path = workspace / GATEWAY_LOCK_FILENAME
        if not lock_path.exists():
            return None
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0) or 0) if isinstance(data, dict) else 0
            if pid <= 0 or not is_process_alive(pid):
                return None
            # PID is alive, but the OS lock may have been released after a
            # crash. Probe the companion OS lock: if we can acquire it, no
            # Gateway actually holds the workspace despite the stale metadata.
            if not GatewayLock._is_os_lock_held(workspace):
                return None
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, IOError):
            return None

    @staticmethod
    def _is_os_lock_held(workspace: Path) -> bool:
        """Non-blocking probe of the companion OS lock file.

        Returns True if the OS lock is genuinely held by another process.
        Returns False if we can acquire it (no holder, or holder crashed).
        """
        os_lock_path = workspace / (GATEWAY_LOCK_FILENAME + ".lock")
        probe = portalocker.Lock(
            str(os_lock_path),
            mode="a+",
            timeout=None,
            check_interval=0.2,
            fail_when_locked=True,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        try:
            probe.acquire()
        except (portalocker.exceptions.AlreadyLocked, portalocker.exceptions.LockException):
            return True
        except OSError:
            # On Windows mandatory locking, even opening the locked file may
            # fail. Treat as held (safe default: avoid false-negative that
            # would let a second Gateway start).
            return True
        try:
            probe.release()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        return False

    def _read_holder(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, IOError):
            return None


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

    Uses atomic write: write to temp file then rename.

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

    # Atomic write: temp file + rename
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = pid_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # On Windows, need to remove existing file first
    if pid_path.exists():
        pid_path.unlink()
    temp_path.rename(pid_path)

    logger.info(
        "Wrote PID file for instance '%s': pid=%d, path=%s",
        config.name, pid, pid_path
    )


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
