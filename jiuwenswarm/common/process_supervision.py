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
from collections.abc import Mapping, Sequence
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

# NTSTATUS error codes: access violation, stack overflow, heap corruption, ...
_WINDOWS_CRASH_FLOOR = 0xC0000000
_WINDOWS_CTRL_C_EXIT = 0xC000013A
_POSIX_CRASH_SIGNALS = frozenset(
    getattr(signal, name)
    for name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL", "SIGFPE")
    if hasattr(signal, name)
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


@contextlib.contextmanager
def application_shutdown():
    """Keep repeated console signals from interrupting bounded final cleanup."""
    import threading

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def signal_service_tree(pid: int, *, force: bool = False, expected_created: float | None = None) -> list[Any]:
    """Signal an owned launcher and proven services, retaining Worker branches."""
    import psutil

    invalid_pid_type = not isinstance(pid, int) or isinstance(pid, bool)
    if invalid_pid_type or pid <= 0 or pid == os.getpid():
        raise RuntimeError("Service tree cleanup refused an invalid launcher PID")
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    except psutil.Error as exc:
        raise RuntimeError("Service launcher identity could not be inspected") from exc
    if expected_created is not None and parent.create_time() != expected_created:
        raise RuntimeError("Service launcher PID identity changed")
    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    except psutil.Error as exc:
        # The root is a caller-owned launcher; its uninspectable children
        # cannot be signalled, but failure must not skip the trusted root.
        children = []
        errors = [exc]
    else:
        errors = []
    retained = set()
    parents = {}
    by_pid = {child.pid: child for child in children}
    for child in children:
        try:
            argv = child.cmdline()
            if "-m" in argv[:-1] and argv[argv.index("-m") + 1] == "jiuwenswarm.server.worker.main":
                retained.add(child.pid)
            parents[child.pid] = child.ppid()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            retained.add(child.pid)
            errors.append(exc)
    for child in children:
        ancestor = parents.get(child.pid)
        visited = {child.pid}
        while ancestor in by_pid and ancestor not in retained:
            if ancestor in visited:
                retained.add(child.pid)
                errors.append(RuntimeError("Service process ancestry contains a cycle"))
                break
            visited.add(ancestor)
            ancestor = parents.get(ancestor)
        if ancestor in retained:
            retained.add(child.pid)
    targets = [child for child in reversed(children) if child.pid not in retained] + [parent]
    for process in targets:
        try:
            process.kill() if force else process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(
            "Service process tree cleanup incomplete; ownership or termination checks failed"
        ) from errors[0]
    return targets


def terminate_service_tree(pid: int, *, timeout: float = 10.0, expected_created: float | None = None) -> None:
    """Observe service exit before the application retires its detached Worker."""
    import psutil

    deadline = time.monotonic() + max(.001, timeout)
    targets = signal_service_tree(pid, expected_created=expected_created)
    _, alive = psutil.wait_procs(targets, timeout=max(0.0, deadline - time.monotonic() - .5))
    errors = []
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            errors.append(exc)
    _, alive = psutil.wait_procs(alive, timeout=max(0.0, deadline - time.monotonic()))
    if errors:
        raise RuntimeError("Service process termination failed") from errors[0]
    if alive:
        raise RuntimeError("Service process exit could not be observed before the shutdown deadline")


def _terminate_all(procs: list[subprocess.Popen]) -> None:
    errors = []
    with application_shutdown():
        for proc in procs:
            if proc.poll() is None:
                try:
                    terminate_service_tree(proc.pid, timeout=_TERMINATE_GRACE)
                except (OSError, RuntimeError) as exc:
                    errors.append(exc)
    if errors:
        raise RuntimeError("Application services did not completely stop") from errors[0]


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
            os.killpg(proc.pid, signal.SIGKILL)


def supervise(commands: Mapping[str, Sequence[str]], popen_kwargs: Mapping[str, Any]) -> int:
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
        _terminate_all([child.proc for child in children if child.proc is not None])
