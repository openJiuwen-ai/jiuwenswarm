# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Per-workspace lock that keeps a single Gateway on one cron store.

Owned by the gateway package so startup does not import
``jiuwenswarm.instance_manager``. The lock filenames and holder JSON are the
mutual-exclusion contract shared with the desktop preflight; do not fork them.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import portalocker

logger = logging.getLogger(__name__)

# Lock filename for per-workspace Gateway singleton (held for entire lifetime)
GATEWAY_LOCK_FILENAME = ".gateway.lock"
# Max seconds to wait for a (possibly shutting-down) Gateway to release the lock
GATEWAY_LOCK_ACQUIRE_TIMEOUT = 30.0


def _system_executable(name: str) -> str:
    """Resolve a system tool the same way instance PID checks do on Windows."""
    if platform.system().lower() == "windows":
        system32 = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32"
        )
        exe_path = os.path.join(system32, f"{name}.exe")
        if os.path.exists(exe_path):
            return exe_path
    return shutil.which(name) or name


def _is_process_alive(pid: int) -> bool:
    """Return whether ``pid`` is a running process."""
    if pid <= 0:
        return False
    if platform.system().lower() == "windows":
        try:
            result = subprocess.run(
                [_system_executable("tasklist"), "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout and "INFO:" not in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _holder_pid(data: Any) -> int:
    """Best-effort holder pid; malformed metadata yields 0 (lock looks free).

    Mirrors Electron's ``Number(data.pid) || 0``: a hand-edited or partially
    written holder file must degrade to "no holder" instead of raising.
    """
    try:
        return int(data.get("pid", 0) or 0) if isinstance(data, dict) else 0
    except (ValueError, TypeError):
        return 0


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
                lock.acquire()
            except portalocker.exceptions.AlreadyLocked:
                holder = self._read_holder()
                pid = _holder_pid(holder)
                if pid == os.getpid():
                    # Self-owned: an os.execv restart reuses the same PID while
                    # the previous image's lock may still be held. We already
                    # own the workspace — treat as acquired.
                    logger.info(
                        "Gateway lock already held by this process, path=%s",
                        self.lock_path,
                    )
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
            logger.info(
                "Gateway lock acquired, pid=%d, path=%s",
                os.getpid(),
                self.lock_path,
            )
            return True

    def release(self) -> None:
        """Release the lock if we own it."""
        if self._lock is None:
            return
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
            logger.debug("Gateway lock marker clear error (ignored): %s", exc)
        try:
            self._lock.release()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Gateway lock release error (ignored): %s", exc)
        self._lock = None

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
            pid = _holder_pid(data)
            if pid <= 0 or not _is_process_alive(pid):
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
