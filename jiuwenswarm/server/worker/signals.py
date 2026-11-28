# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Readiness events the Worker emits toward the Front."""

from __future__ import annotations

import asyncio
import logging
import time

from jiuwenswarm.server.ipc.protocol import KIND_READINESS
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder

logger = logging.getLogger(__name__)


def _trace_recorder() -> object | None:
    """Return the active recorder without importing lifecycle when disabled."""
    try:
        from jiuwenswarm.runtime_trace.state import current_recorder

        return current_recorder()
    except Exception:  # noqa: BLE001 - tracing must not break readiness signals
        return None


class RuntimeSignalSink:
    """Sync hooks Agent Runtime already calls. Sends do not block startup."""

    def __init__(self, holder: ConnHolder) -> None:
        self._holder = holder

    def agent_ready(self) -> None:
        self._record_agent_ready_wait()
        self._finish_startup("success", None)
        self._emit("AGENT_READY", "")

    def warmup_retry(self, reason: str) -> None:
        self._startup_event("startup.warmup_retry")
        self._emit("RUNTIME_WARMING", reason)

    def failed(self, reason: str) -> None:
        recorder = _trace_recorder()
        handle = getattr(recorder, "startup_handle", None) if recorder else None
        if handle is not None and not handle.ended:
            self._finish_startup("error", "runtime_load_failed")
        else:
            self._startup_event("startup.failed")
        self._emit("FAILED", reason)

    def degraded(self, reason: str) -> None:
        self._startup_event("startup.degraded")
        self._emit("DEGRADED", reason)

    def _record_agent_ready_wait(self) -> None:
        """Account for the wait between the last startup stage and readiness."""
        try:
            recorder = _trace_recorder()
            handle = getattr(recorder, "startup_handle", None) if recorder else None
            last_stage = getattr(recorder, "last_stage_mono", None)
            if (
                handle is None
                or handle.ended
                or not isinstance(last_stage, int)
            ):
                return
            now_mono = time.monotonic_ns()
            if now_mono <= last_stage:
                return
            wait = recorder.start(
                "startup.post.agent_ready",
                parent=handle,
                level="normal",
                mono_start=last_stage,
            )
            recorder.end(wait, "success", mono_end=now_mono)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Worker] runtime trace agent_ready wait failed", exc_info=True
            )

    def _finish_startup(self, outcome: str, reason_code: str | None) -> None:
        try:
            recorder = _trace_recorder()
            handle = getattr(recorder, "startup_handle", None) if recorder else None
            if handle is None or handle.ended:
                return
            attributes = (
                {"runtime.reason_code": reason_code} if reason_code else None
            )
            recorder.end(handle, outcome, attributes=attributes)
        except Exception:  # noqa: BLE001
            logger.debug("[Worker] runtime trace startup finish failed", exc_info=True)

    def _startup_event(self, name: str) -> None:
        try:
            recorder = _trace_recorder()
            handle = getattr(recorder, "startup_handle", None) if recorder else None
            if handle is None:
                return
            recorder.event(handle, name)
        except Exception:  # noqa: BLE001
            logger.debug("[Worker] runtime trace event failed", exc_info=True)

    def _emit(self, state: str, reason: str) -> None:
        message = {"kind": KIND_READINESS, "state": state, "reason": reason}
        self._holder.remember(message)
        conn = self._holder.conn
        if conn is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_send(conn, message))

    async def _safe_send(self, conn: object, message: dict) -> None:
        send = getattr(conn, "send", None)
        if not callable(send):
            return
        try:
            await send(message)
        except Exception:  # noqa: BLE001
            logger.warning("[Worker] readiness send failed", exc_info=True)
