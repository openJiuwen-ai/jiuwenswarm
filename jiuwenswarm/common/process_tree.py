# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminate a process together with its descendants.

Windows does not kill grandchildren when the parent is ``TerminateProcess``'d.
``jiuwenswarm-start`` only holds ``app.py``; AgentServer and jiuwenbox sit
underneath and would otherwise stay running after the launcher exits.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)


def collect_descendant_pids(pid: int) -> list[int]:
    """Snapshot recursive children of ``pid``. Empty if the process is gone."""
    if not pid or pid <= 0:
        return []
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return []
    try:
        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[process_tree] children pid=%s failed: %s", pid, exc)
        return []


def pid_is_running(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return _pid_exists_fallback(pid)
    return bool(psutil.pid_exists(pid))


def terminate_pid_tree(pid: int, *, force: bool = False) -> None:
    """Signal ``pid`` and every descendant. Best-effort; never raises."""
    if not pid or pid <= 0:
        return
    try:
        import psutil
    except Exception:  # noqa: BLE001
        _terminate_pid_fallback(pid, force=force)
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("[process_tree] lookup pid=%s failed: %s", pid, exc)
        _terminate_pid_fallback(pid, force=force)
        return
    try:
        children = parent.children(recursive=True)
    except Exception:  # noqa: BLE001
        children = []
    kill_fn = (lambda proc: proc.kill()) if force else (lambda proc: proc.terminate())
    for child in reversed(children):
        try:
            kill_fn(child)
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("[process_tree] child pid=%s: %s", child.pid, exc)
    try:
        kill_fn(parent)
    except psutil.NoSuchProcess:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("[process_tree] parent pid=%s: %s", pid, exc)


def terminate_popen_tree(proc: Any, *, force: bool = False) -> None:
    """Same as :func:`terminate_pid_tree` for a ``subprocess.Popen``."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:  # noqa: BLE001
        pass
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    terminate_pid_tree(int(pid), force=force)


def _pid_exists_fallback(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _terminate_pid_fallback(pid: int, *, force: bool) -> None:
    if sys.platform == "win32":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
        return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass
