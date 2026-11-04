# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run designer ffmpeg work off the Runtime Worker event loop.

The Worker answers Supervisor heartbeats from its asyncio loop, so waiting on
ffmpeg there for longer than the heartbeat deadline gets the whole Worker
restarted (``runtime worker closed`` for every in-flight request). Media
helpers stay synchronous; async callers run them through :func:`run_off_loop`,
which also kills the ffmpeg processes they started once the awaiting task is
cancelled.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import threading
from typing import Any, Callable, TypeVar

# Stream inspection (``ffmpeg -i``) and frame grabs finish in well under a second.
PROBE_TIMEOUT_S = 60.0
# Concat / re-encode / audio mix: only a hung ffmpeg should reach this.
ENCODE_TIMEOUT_S = 1800.0

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_T = TypeVar("_T")


class FfmpegCancelled(RuntimeError):
    """The task awaiting this media work was cancelled; no further ffmpeg starts."""


class _ProcessScope:
    """ffmpeg processes started by one :func:`run_off_loop` call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen[str]] = set()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def register(self, proc: subprocess.Popen[str]) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._procs.add(proc)
            return True

    def discard(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.discard(proc)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)


_SCOPE: contextvars.ContextVar[_ProcessScope | None] = contextvars.ContextVar(
    "designer_ffmpeg_scope", default=None
)


def _kill(proc: subprocess.Popen[str]) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def run_ffmpeg(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one ffmpeg command to completion, killing it after ``timeout`` seconds.

    Blocking; call it from a :func:`run_off_loop` thread, never on the event loop.
    stdin is closed so ffmpeg never waits for interactive keys.

    Raises:
        RuntimeError: ffmpeg did not finish within ``timeout``.
        FfmpegCancelled: the owning task was cancelled before or while it ran.
    """
    scope = _SCOPE.get()
    if scope is not None and scope.cancelled:
        raise FfmpegCancelled("ffmpeg not started: media task cancelled")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc: subprocess.Popen[str] = subprocess.Popen(cmd, **kwargs)
    if scope is not None and not scope.register(proc):
        _kill(proc)
        proc.communicate()
        raise FfmpegCancelled("ffmpeg stopped: media task cancelled")
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        proc.communicate()
        raise RuntimeError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    except BaseException:
        _kill(proc)
        proc.communicate()
        raise
    finally:
        if scope is not None:
            scope.discard(proc)
    if scope is not None and scope.cancelled:
        raise FfmpegCancelled("ffmpeg stopped: media task cancelled")
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


async def run_off_loop(func: Callable[..., _T], *args: Any) -> _T:
    """Await ``func(*args)`` on a worker thread; cancelling kills its ffmpeg.

    ``func`` must not touch state the event loop mutates concurrently: hand it
    paths or snapshots, not the live graph / run records.
    """
    scope = _ProcessScope()

    def call() -> _T:
        # asyncio.to_thread runs this in a copied context, so the scope never
        # leaks back into the awaiting task.
        _SCOPE.set(scope)
        return func(*args)

    try:
        return await asyncio.to_thread(call)
    except asyncio.CancelledError:
        scope.cancel()
        raise


__all__ = [
    "ENCODE_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "FfmpegCancelled",
    "run_ffmpeg",
    "run_off_loop",
]
