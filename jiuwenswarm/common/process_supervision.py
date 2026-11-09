# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Supervise the AgentServer and Gateway children of ``jiuwenswarm.app``.

Every stop path ends a child and relies on the supervisor then tearing the rest
down: ``jiuwenswarm-start --stop`` kills one port owner (SIGTERM/SIGKILL, or
``taskkill /F`` on Windows), and the pip upgrade makes the Gateway SIGTERM
itself. So only exits that cannot come from a stop path are respawned; anything
else keeps the historical "one child exits => everything stops" behaviour.

SIGKILL is the one exception (issue #4946): it is also what the OOM killer and
infrastructure send, and reading it as a stop path turned every external hard
kill into a full-stack teardown. A SIGKILLed child is respawned under the same
uptime/budget gates as a crash; see ``_Child.should_respawn``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger("jiuwenswarm.app")

# A supervised Gateway exits with this code to ask for a respawn (config reload
# failure). os.execv cannot be used under a supervisor: on Windows it starts a
# new PID and exits the old one with 0, which reads as the Gateway stopping.
GATEWAY_RESTART_EXIT_CODE = 75
# Children exit with this code (EX_SOFTWARE) when debug_dump's crash handler
# catches an unhandled main-thread exception. Windows cannot tell a plain
# Python fatal exit (1) from taskkill /F (1), so a crash the supervisor should
# respawn has to announce itself; native crashes bypass Python entirely and
# arrive as NTSTATUS codes instead.
CRASH_EXIT_CODE = 70
# Set to the supervisor's PID so a child can tell it is supervised, even when
# the variable leaks into an unrelated process through a saved environment.
SUPERVISOR_PID_ENV = "JIUWENSWARM_SUPERVISOR_PID"

_POLL_INTERVAL = 0.25
_RESTART_BACKOFF = 2.0
# A .restart_pending.json marker from a failed upgrade is never cleaned up, and
# Windows recycles PIDs fast; only trust it while a relaunch could still be in
# flight, so a stale marker cannot suppress genuine crash respawns forever.
_UPGRADE_MARKER_TTL = 600.0
# Earlier exits are startup failures (lock held, port taken, bad config) that a
# respawn would only repeat.
_MIN_UPTIME_FOR_RESTART = 30.0
_RESTART_WINDOW = 600.0
_MAX_CRASH_RESTARTS = 3
_MAX_RESTART_REQUESTS = 5
_TERMINATE_GRACE = 12.0
_DESCENDANT_TERMINATE_GRACE = 5.0

# NTSTATUS error codes: access violation, stack overflow, heap corruption, ...
_WINDOWS_CRASH_FLOOR = 0xC0000000
_WINDOWS_CTRL_C_EXIT = 0xC000013A
# Resolve POSIX signals by name: SIGBUS is 7 on Linux but 10 on macOS, while
# 7 means SIGEMT on the latter. Windows never executes this branch, so use the
# POSIX fallbacks there to keep its simulated POSIX tests deterministic.
_POSIX_CRASH_SIGNAL_FALLBACKS = (
    ("SIGILL", 4),
    ("SIGABRT", 6),
    ("SIGBUS", 7),
    ("SIGFPE", 8),
    ("SIGSEGV", 11),
)
_POSIX_CRASH_SIGNALS = frozenset(
    fallback if sys.platform == "win32" else int(getattr(signal, name, fallback))
    for name, fallback in _POSIX_CRASH_SIGNAL_FALLBACKS
)
# POSIX reserves 9 for SIGKILL; Windows lacks the constant (a hard kill reads
# as exit code 1 there), so the literal keeps the gate testable everywhere.
_SIGKILL = getattr(signal, "SIGKILL", 9)


def is_crash_exit(returncode: int) -> bool:
    """Return whether *returncode* can only mean a crash, never a stop path."""
    if sys.platform == "win32":
        # taskkill /F exits 1 and os.kill(SIGTERM) exits 15; both are stop paths.
        if returncode == CRASH_EXIT_CODE:
            return True
        return returncode >= _WINDOWS_CRASH_FLOOR and returncode != _WINDOWS_CTRL_C_EXIT
    if returncode < 0:
        return -returncode in _POSIX_CRASH_SIGNALS
    # 2 is an argparse usage error; >= 128 follows the shell's signal convention.
    return returncode not in (0, 2, GATEWAY_RESTART_EXIT_CODE) and returncode < 128


def _describe_exit(returncode: int) -> str:
    if returncode < 0:
        try:
            return f"{returncode} ({signal.Signals(-returncode).name})"
        except ValueError:
            return str(returncode)
    if returncode >= _WINDOWS_CRASH_FLOOR:
        return f"{returncode} ({returncode:#010x})"
    return str(returncode)


def _upgrade_pending_for(pid: int) -> bool:
    """Whether the pip upgrade asked *pid* to exit so it can relaunch the service."""
    from jiuwenswarm.common.upgrade_executor import restart_pending_path

    try:
        data = json.loads(restart_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("pid") != pid:
        return False
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return False
    return time.time() - timestamp < _UPGRADE_MARKER_TTL


def _breadcrumb_path():
    from jiuwenswarm.common.utils import get_logs_dir

    return get_logs_dir() / "service_exits.jsonl"


def _record_exit_breadcrumb(
    name: str, pid: int | None, returncode: int, uptime: float, action: str
) -> None:
    """Persist one line per child exit so post-mortems survive a lost console.

    Windows service-form runs often capture nothing of the launcher console,
    and the child itself may die without a stack (native crash, hard kill).
    This file is the durable record of who exited when, with which code, and
    what the supervisor decided.
    """
    try:
        path = _breadcrumb_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "service": name,
                        "pid": pid,
                        "returncode": returncode,
                        "uptime_seconds": round(uptime, 1),
                        "action": action,
                    }
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 - diagnostics must never break supervision
        logger.debug("[app] exit breadcrumb write failed", exc_info=True)


def _within_budget(history: deque[float], limit: int, now: float) -> bool:
    while history and now - history[0] > _RESTART_WINDOW:
        history.popleft()
    if len(history) >= limit:
        return False
    history.append(now)
    return True


def _process_create_time(pid: int | None) -> float | None:
    if pid is None:
        return None
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


class _Child:
    def __init__(self, name: str, command: Sequence[str]) -> None:
        self.name = name
        self.command = list(command)
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.create_time: float | None = None
        self.own_session = False
        self.crash_restarts: deque[float] = deque()
        self.restart_requests: deque[float] = deque()

    def spawn(self, popen_kwargs: Mapping[str, Any]) -> None:
        self.proc = subprocess.Popen(self.command, **popen_kwargs)
        self.started_at = time.monotonic()
        self.create_time = _process_create_time(self.proc.pid)
        self.own_session = bool(popen_kwargs.get("start_new_session"))

    def should_respawn(self, returncode: int, uptime: float) -> bool:
        now = time.monotonic()
        if returncode == GATEWAY_RESTART_EXIT_CODE:
            if self.proc is not None and _upgrade_pending_for(self.proc.pid):
                # A pip upgrade is about to relaunch the full stack; respawning
                # here would race updater_restart_helper into a second instance
                # fighting over the same ports.
                return False
            if _within_budget(self.restart_requests, _MAX_RESTART_REQUESTS, now):
                return True
            logger.error("[app] %s requested too many restarts; shutting down", self.name)
            return False
        if not is_crash_exit(returncode):
            # SIGKILL cannot be classified as a crash (stop paths escalate to it
            # after a SIGTERM that did not take effect), but reading it as a stop
            # path tore the whole service down on every external hard kill — OOM
            # killer, infra (issue #4946). Respawn it under the same gates as a
            # crash; the restart budget bounds the churn a stop-path SIGKILL can
            # cause.
            if returncode >= 0 or -returncode != _SIGKILL:
                return False
        if uptime < _MIN_UPTIME_FOR_RESTART:
            return False
        if self.proc is not None and _upgrade_pending_for(self.proc.pid):
            return False
        if _within_budget(self.crash_restarts, _MAX_CRASH_RESTARTS, now):
            return True
        logger.error(
            "[app] %s crashed %d times within %.0fs; shutting down",
            self.name,
            _MAX_CRASH_RESTARTS + 1,
            _RESTART_WINDOW,
        )
        return False


def _terminate_all(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + _TERMINATE_GRACE
    while time.monotonic() < deadline:
        if all(proc.poll() is not None for proc in procs):
            break
        time.sleep(0.1)
    for proc in procs:
        if proc.poll() is None:
            proc.kill()


def _capture_lingering_descendants(children: Sequence[_Child]) -> list[tuple[int, float]]:
    """Capture descendants that must not outlive one selected service restart."""
    import psutil

    identities: list[tuple[int, float]] = []
    for child in children:
        proc = child.proc
        if proc is None or proc.pid is None or child.create_time is None:
            continue
        try:
            parent = psutil.Process(proc.pid)
            if parent.create_time() != child.create_time:
                continue
            descendants = parent.children(recursive=True)
        except psutil.Error:
            continue
        for descendant in descendants:
            try:
                identities.append((descendant.pid, descendant.create_time()))
            except psutil.Error:
                continue
    return identities


def _matching_descendants(identities: Sequence[tuple[int, float]]) -> list[Any]:
    """Return live descendants after verifying each PID was not recycled."""
    import psutil

    processes: list[Any] = []
    for pid, create_time in identities:
        try:
            process = psutil.Process(pid)
            if process.create_time() == create_time:
                processes.append(process)
        except psutil.Error:
            continue
    return processes


def _terminate_lingering_descendants(identities: Sequence[tuple[int, float]]) -> None:
    """End verified descendants left by a selected service restart."""
    processes = _matching_descendants(identities)
    if not processes:
        return
    logger.info("[app] terminating %s lingering service descendants", len(processes))
    for process in processes:
        with contextlib.suppress(Exception):
            process.terminate()
    deadline = time.monotonic() + _DESCENDANT_TERMINATE_GRACE
    while processes and time.monotonic() < deadline:
        time.sleep(0.1)
        processes = [process for process in processes if _is_process_running(process)]
    for process in processes:
        with contextlib.suppress(Exception):
            process.kill()


def _is_process_running(process: Any) -> bool:
    """Return whether a captured descendant still exists without racing exit."""
    try:
        return bool(process.is_running()) and process.status() != "zombie"
    except Exception:  # noqa: BLE001 - a terminated descendant is already reaped
        return False


def _reap_lingering_tree(child: _Child) -> None:
    """Kill descendants a hard-killed child left behind, before respawning it.

    Otherwise the replacement inherits held ports/locks, fails within seconds
    and the uptime gate turns one crash into a permanent teardown. On Windows
    the dead PID stays pinned by our open Popen handle and create_time proves
    it was not recycled; on POSIX the child leads its own session, and a pgid
    is never recycled while any member lives, so killpg can only hit the dead
    child's own tree (and no-ops with ESRCH once nothing is left).
    """
    proc = child.proc
    if proc is None or proc.pid is None:
        return
    if sys.platform == "win32":
        if child.create_time is None:
            return
        import psutil

        try:
            dead = psutil.Process(proc.pid)
            if dead.create_time() != child.create_time:
                return  # PID already recycled; those descendants are a stranger's
            descendants = dead.children(recursive=True)
        except psutil.Error:
            return
        for victim in reversed(descendants):
            with contextlib.suppress(psutil.Error):
                victim.kill()
    elif child.own_session:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, _SIGKILL)


def supervise(
    commands: Mapping[str, Sequence[str]],
    popen_kwargs: Mapping[str, Any],
    *,
    restart_requested: Callable[[], bool] | None = None,
    restart_names: frozenset[str] = frozenset(),
    terminate_descendants_on_restart: frozenset[str] = frozenset(),
) -> int:
    """Run *commands* (spawned in order) until one stops for good; return its exit code.

    Returns 130 on KeyboardInterrupt. All children are terminated on return.
    """
    children = [_Child(name, command) for name, command in commands.items()]
    try:
        # Spawning happens inside the try so that a signal (or a failing later
        # Popen) arriving between spawns still tears the earlier ones down.
        # KeyboardInterrupt is a BaseException, so an `except Exception` guard
        # around a spawn would have let it orphan the children already started.
        for child in children:
            child.spawn(popen_kwargs)
        while True:
            if restart_requested is not None and restart_requested():
                restarted = _restart_selected_children(
                    children,
                    popen_kwargs,
                    restart_names,
                    terminate_descendants_on_restart,
                )
                if restarted:
                    continue
            for child in children:
                proc = child.proc
                if proc is None:
                    continue
                returncode = proc.poll()
                if returncode is None:
                    continue
                uptime = time.monotonic() - child.started_at
                logger.warning(
                    "[app] %s (pid %s) exited with code %s after %.0fs",
                    child.name,
                    proc.pid,
                    _describe_exit(returncode),
                    uptime,
                )
                respawn = child.should_respawn(returncode, uptime)
                _record_exit_breadcrumb(
                    child.name,
                    proc.pid,
                    returncode,
                    uptime,
                    "respawn" if respawn else "teardown",
                )
                if not respawn:
                    return returncode or 0
                _reap_lingering_tree(child)
                time.sleep(_RESTART_BACKOFF)
                child.spawn(popen_kwargs)
                new_proc = child.proc
                logger.warning(
                    "[app] %s respawned (pid %s)",
                    child.name,
                    new_proc.pid if new_proc is not None else "?",
                )
            time.sleep(_POLL_INTERVAL)
    except KeyboardInterrupt:
        return 130
    finally:
        # Runtime Worker deliberately runs in its own session so an
        # AgentServer Front restart can reuse it. A complete supervisor
        # shutdown must nevertheless match a cold start: capture descendants
        # before ending the Fronts, then reap the detached Worker as well.
        descendants = _capture_lingering_descendants(children)
        _terminate_all([child.proc for child in children if child.proc is not None])
        _terminate_lingering_descendants(descendants)


def _restart_selected_children(
    children: Sequence[_Child],
    popen_kwargs: Mapping[str, Any],
    names: frozenset[str],
    terminate_descendants: frozenset[str],
) -> bool:
    """Restart selected healthy children without ending the supervisor."""
    selected = [child for child in children if child.name in names]
    if not selected:
        return False
    logger.info("[app] restarting requested children: %s", ", ".join(child.name for child in selected))
    descendants = _capture_lingering_descendants(
        [child for child in selected if child.name in terminate_descendants]
    )
    _terminate_all([child.proc for child in selected if child.proc is not None])
    _terminate_lingering_descendants(descendants)
    for child in selected:
        child.spawn(popen_kwargs)
        logger.info(
            "[app] %s restarted (pid %s)",
            child.name,
            child.proc.pid if child.proc is not None else "?",
        )
    return True
