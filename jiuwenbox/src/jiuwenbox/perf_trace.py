# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Optional per-step timing for sandbox creation.

Collection is off unless :func:`configure` is called with a log path.
The disabled path does not sample the clock and does not touch the filesystem.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

ENV_PERF_LOG = "JIUWENBOX_PERF_LOG"

_lock = threading.Lock()
_file = None
_t0 = 0.0


class _NullSpan:
    """Context manager / token used when collection is off."""

    def __enter__(self) -> _NullSpan:
        return self

    @staticmethod
    def __exit__(*_exc: object) -> bool:
        return False

    @staticmethod
    def close() -> None:
        return None


_NULL = _NullSpan()


class _Span:
    def __init__(self, step: str, sandbox_id: str) -> None:
        self._step = step
        self._sandbox_id = sandbox_id
        self._start: float | None = None

    def __enter__(self) -> _Span:
        self.note_start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def note_start(self) -> None:
        self._start = time.perf_counter()

    def close(self) -> None:
        if self._start is None:
            self.note_start()
        _write(self._step, self._sandbox_id, self._start, time.perf_counter())


def enabled() -> bool:
    return _file is not None


def configure(path: str | None) -> None:
    """Open ``path`` for append, or disable collection when ``path`` is empty."""
    global _file, _t0
    with _lock:
        if _file is not None:
            _file.close()
            _file = None
        if not path:
            return
        dest = Path(path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        handle = dest.open("a", encoding="utf-8", buffering=1)
        release = True
        try:
            _t0 = time.perf_counter()
            handle.write(
                "# start_ms elapsed_ms sandbox_id step\n"
                "# start_ms is monotonic milliseconds since collection was enabled.\n"
                "# Nested steps share the timeline; do not add a parent to its children.\n"
            )
            _file = handle
            release = False
        finally:
            if release:
                handle.close()


def configure_from_env() -> None:
    """Enable collection when ``--perf`` or ``JIUWENBOX_PERF_LOG`` set a path."""
    raw = os.environ.get(ENV_PERF_LOG, "").strip()
    configure(raw or None)


def span(step: str, sandbox_id: str = "-") -> _NullSpan | _Span:
    """Time ``step`` for ``sandbox_id``. A no-op when collection is off."""
    if _file is None:
        return _NULL
    return _Span(step, sandbox_id or "-")


def record_elapsed(step: str, sandbox_id: str, elapsed_ms: float) -> None:
    """Record a duration measured in another process.

    ``start_ms`` is placed so the span ends when the parent observed it.
    A no-op when collection is off, without sampling the clock.
    """
    if _file is None:
        return
    end = time.perf_counter()
    start = end - (max(elapsed_ms, 0.0) / 1000.0)
    _write(step, sandbox_id or "-", start, end)


def begin(step: str, sandbox_id: str = "-") -> _NullSpan | _Span:
    """Start a span that the caller closes, including on failure."""
    if _file is None:
        return _NULL
    started = _Span(step, sandbox_id or "-")
    started.note_start()
    return started


def _write(step: str, sandbox_id: str, start: float, end: float) -> None:
    handle = _file
    if handle is None:
        return
    line = (
        f"{(start - _t0) * 1000:.3f}\t"
        f"{(end - start) * 1000:.3f}\t"
        f"{sandbox_id}\t"
        f"{step}\n"
    )
    with _lock:
        handle.write(line)
