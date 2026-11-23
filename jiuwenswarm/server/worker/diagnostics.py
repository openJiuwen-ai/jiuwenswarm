# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leave native thread stacks before Supervisor times out a stalled Worker."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import time
from contextlib import AbstractContextManager, ExitStack, nullcontext
from typing import Any, TextIO

from jiuwenswarm.server.ipc.protocol import KIND_HEARTBEAT
from jiuwenswarm.server.worker.stall_context import BusinessTracker

_STALL_DUMP_TIMEOUT = 25.0  # Before the Supervisor's 30-second health deadline.
_enabled = False
_last_arm = 0.0
_fault_output: TextIO | None = None
_fault_resources = ExitStack()


def enable_worker_diagnostics() -> None:
    global _enabled, _fault_output, _fault_resources
    from jiuwenswarm.common.paths import get_agent_logs_dir

    if _enabled:
        return
    try:
        directory = get_agent_logs_dir() / "runtime_diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(directory.glob("worker-*.fault.log"), key=lambda path: path.stat().st_mtime)
        for old in existing[:-7]:
            old.unlink(missing_ok=True)
        path = directory / f"worker-{os.getpid()}-{time.time_ns()}.fault.log"
        with ExitStack() as resources:
            output = resources.enter_context(path.open("a", encoding="utf-8"))
            faulthandler.enable(file=output, all_threads=True)
            _fault_output = output
            _fault_resources = resources.pop_all()
        logging.getLogger(__name__).info("[Worker] native fault stacks=%s", path)
    except OSError:
        # Diagnostic storage failure must never prevent runtime startup.
        faulthandler.enable(all_threads=True)
    _enabled = True


def disable_worker_diagnostics() -> None:
    """Release the process-owned fault file after native handlers stop using it."""
    global _enabled, _fault_output, _last_arm
    try:
        if _enabled:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
    finally:
        _enabled = False
        _last_arm = 0.0
        _fault_output = None
        _fault_resources.close()


atexit.register(disable_worker_diagnostics)


def arm_stall_dump(*, beat_age: float | None = None) -> None:
    global _last_arm
    now = time.monotonic()
    if beat_age is not None and beat_age >= min(5.0, _STALL_DUMP_TIMEOUT / 2):
        # Off-loop pongs must not postpone the native dump of a stalled loop.
        return
    if _enabled and now - _last_arm >= min(5.0, _STALL_DUMP_TIMEOUT / 2):
        try:
            if _fault_output is not None and os.fstat(_fault_output.fileno()).st_size > 5 * 1024 * 1024:
                faulthandler.cancel_dump_traceback_later()
                _fault_output.seek(0)
                _fault_output.truncate()
            # This native timer fires even when Python's event loop is blocked.
            if _fault_output is None:
                faulthandler.dump_traceback_later(_STALL_DUMP_TIMEOUT)
            else:
                faulthandler.dump_traceback_later(_STALL_DUMP_TIMEOUT, file=_fault_output)
        except (OSError, ValueError, RuntimeError):
            # Disk/storage faults must not break the heartbeat that diagnoses them.
            logging.getLogger(__name__).warning("[Worker] could not arm fault diagnostics", exc_info=True)
        _last_arm = now


def cancel_stall_dump() -> None:
    global _last_arm
    if _enabled:
        faulthandler.cancel_dump_traceback_later()
        _last_arm = 0.0


class LoopBeat:
    """Event-loop liveness that the heartbeat fast path can read off-loop.

    The async beat task refreshes the timestamp; a blocked loop lets it age.
    The reader thread reports that age in every pong, so transport liveness
    and business-loop responsiveness stay distinguishable.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._tracker: BusinessTracker | None = None

    def enable_tracking(self) -> None:
        self._tracker = BusinessTracker()

    def close_tracking(self) -> None:
        if self._tracker is not None:
            self._tracker.close()

    def activity(self, metadata: dict[str, str]) -> AbstractContextManager[None]:
        return self._tracker.activity(metadata) if self._tracker is not None else nullcontext()

    def stall_context(self) -> dict[str, Any] | None:
        if self._tracker is None:
            return None
        try:
            return self._tracker.snapshot()
        except Exception:  # noqa: BLE001 - attribution must never stop heartbeats
            return {"operation": "unavailable"}

    def beat(self) -> None:
        self._last = time.monotonic()

    @property
    def age(self) -> float:
        return max(0.0, time.monotonic() - self._last)


def heartbeat_reply(
    beat_age: float | None, *, stall_context: dict[str, Any] | None = None, probe_id: object = None,
) -> dict[str, Any]:
    """Build one heartbeat pong. ``loop_beat_age`` is omitted for legacy pongs."""
    reply: dict[str, Any] = {"kind": KIND_HEARTBEAT, "pid": os.getpid()}
    if isinstance(probe_id, str) and 0 < len(probe_id) <= 64:
        reply["probe_id"] = probe_id
    if beat_age is not None:
        reply["loop_beat_age"] = round(beat_age, 3)
    if stall_context is not None:
        reply["loop_stall"] = stall_context
    return reply
