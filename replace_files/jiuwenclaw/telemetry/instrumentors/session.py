# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for SessionManager lifecycle — SESSION metrics.

Tracks session state transitions (created/active/idle/cancelled/destroyed)
and detects stuck sessions via periodic checking.

Target: ``jiuwenclaw.agentserver.session_manager.SessionManager``
    - ``ensure_session_processor(session_id)``
    - ``cancel_session_task(session_id, log_msg_prefix="")``
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any, Dict

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_SESSION_STATE,
    JIUWENCLAW_SESSION_STATE_REASON,
)
from jiuwenclaw.telemetry.metrics import (
    add_session_created_count,
    add_session_state_count,
    add_session_stuck_count,
    record_session_stuck_age,
    set_session_active_observer,
)

# Module-level config — set by instrument_session()
_stuck_threshold_ms: float = 300000.0
_stuck_check_interval_s: float = 30.0
_tracked_session_managers: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _emit_state(session_id: str, state: str, reason: str) -> None:
    """Record a session state transition."""
    add_session_state_count(1, {
        JIUWENCLAW_SESSION_ID: session_id,
        JIUWENCLAW_SESSION_STATE: state,
        JIUWENCLAW_SESSION_STATE_REASON: reason,
    })


def _count_active_sessions() -> int:
    """Count active session processors across tracked SessionManager instances."""
    active_sessions = 0
    for mgr in list(_tracked_session_managers):
        processors = getattr(mgr, "_session_processors", {}) or {}
        active_sessions += sum(
            1
            for task in processors.values()
            if task is not None and not task.done()
        )
    return active_sessions


def instrument_session(
    stuck_threshold_ms: float = 300000.0,
    stuck_check_interval_s: float = 30.0,
) -> None:
    """Monkey-patch ``SessionManager`` to collect session state/stuck metrics."""
    global _stuck_threshold_ms, _stuck_check_interval_s
    _stuck_threshold_ms = float(stuck_threshold_ms)
    _stuck_check_interval_s = float(stuck_check_interval_s)
    set_session_active_observer(_count_active_sessions)

    try:
        from jiuwenclaw.agentserver.session_manager import SessionManager
    except ImportError:
        logger.debug("[Telemetry] SessionManager not available, skipping session instrumentor")
        return

    # Idempotent guard — apply_instrumentors may be called more than once in tests
    if getattr(SessionManager, "_telemetry_patched", False):
        logger.debug("[Telemetry] SessionManager already instrumented, skipping")
        return

    _original_init = SessionManager.__init__
    _original_ensure_processor = SessionManager.ensure_session_processor
    _original_cancel_task = SessionManager.cancel_session_task

    # --- Patch __init__: add tracking dicts ---
    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self._session_task_start_times: Dict[str, float] = {}
        self._stuck_reported: Dict[str, bool] = {}
        self._stuck_checker_task = None
        _tracked_session_managers.add(self)

    # --- Patch ensure_session_processor: replace with instrumented version ---
    async def _patched_ensure_session_processor(self, session_id: str) -> None:
        if session_id in self._session_processors and not self._session_processors[session_id].done():
            return

        # 创建新的优先级队列和计数器
        self._session_queues[session_id] = asyncio.PriorityQueue()
        self._session_priorities[session_id] = 0

        # >>> 埋点: state=created
        add_session_created_count(1)
        _emit_state(session_id, "created", "new_request")

        async def process_session_queue():
            """处理 session 任务队列，含 telemetry 埋点."""
            queue = self._session_queues[session_id]
            while True:
                try:
                    priority, task_func = await queue.get()
                    if task_func is None:
                        break

                    # >>> 埋点: state=active
                    self._session_task_start_times[session_id] = time.monotonic()
                    self._stuck_reported.pop(session_id, None)
                    _emit_state(session_id, "active", "task_started")

                    self._session_tasks[session_id] = asyncio.create_task(task_func())
                    try:
                        await self._session_tasks[session_id]
                        # >>> 埋点: state=idle, reason=task_completed
                        _emit_state(session_id, "idle", "task_completed")
                    except asyncio.CancelledError:
                        # cancelled 状态在 cancel_session_task patch 中记录
                        pass
                    except Exception:
                        # >>> 埋点: state=idle, reason=task_error
                        _emit_state(session_id, "idle", "task_error")
                    finally:
                        self._session_tasks[session_id] = None
                        self._session_task_start_times.pop(session_id, None)
                        queue.task_done()

                except asyncio.CancelledError:
                    logger.info("[SessionManager] Session 任务处理器被取消: session_id=%s", session_id)
                    break
                except Exception as e:
                    logger.error("[SessionManager] Session 任务处理器异常: %s", e)

            # 清理
            self._session_queues.pop(session_id, None)
            self._session_priorities.pop(session_id, None)
            self._session_tasks.pop(session_id, None)
            self._session_processors.pop(session_id, None)
            self._session_task_start_times.pop(session_id, None)
            self._stuck_reported.pop(session_id, None)

            # >>> 埋点: state=destroyed
            _emit_state(session_id, "destroyed", "queue_closed")
            logger.info("[SessionManager] Session 任务处理器已关闭: session_id=%s", session_id)

        self._session_processors[session_id] = asyncio.create_task(process_session_queue())

        _ensure_stuck_checker(self)

    # --- Patch cancel_session_task: add state=cancelled metric ---
    async def _patched_cancel_session_task(self, session_id: str, log_msg_prefix: str = "") -> None:
        task = self._session_tasks.get(session_id)
        if task is not None and not task.done():
            # >>> 埋点: state=cancelled
            _emit_state(session_id, "cancelled", "user_cancel")
            start_times = getattr(self, "_session_task_start_times", None)
            if start_times is not None:
                start_times.pop(session_id, None)
            stuck_reported = getattr(self, "_stuck_reported", None)
            if stuck_reported is not None:
                stuck_reported.pop(session_id, None)

        await _original_cancel_task(self, session_id, log_msg_prefix)

    # Apply patches
    SessionManager.__init__ = _patched_init
    SessionManager.ensure_session_processor = _patched_ensure_session_processor
    SessionManager.cancel_session_task = _patched_cancel_session_task
    SessionManager._telemetry_patched = True


def _ensure_stuck_checker(session_manager) -> None:
    """Start the periodic stuck session checker if not already running."""
    checker = getattr(session_manager, "_stuck_checker_task", None)
    if checker is not None and not checker.done():
        return

    async def _check_stuck_sessions():
        while True:
            try:
                await asyncio.sleep(_stuck_check_interval_s)
                now = time.monotonic()
                start_times = getattr(session_manager, "_session_task_start_times", {})
                stuck_reported = getattr(session_manager, "_stuck_reported", {})

                for sid, start in list(start_times.items()):
                    age_ms = (now - start) * 1000
                    if age_ms > _stuck_threshold_ms:
                        record_session_stuck_age(age_ms, {JIUWENCLAW_SESSION_ID: sid})
                        if not stuck_reported.get(sid):
                            add_session_stuck_count(1, {JIUWENCLAW_SESSION_ID: sid})
                            stuck_reported[sid] = True
                            logger.warning(
                                "[Telemetry] Session stuck detected: session_id=%s, age_ms=%.0f",
                                sid, age_ms,
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[Telemetry] Stuck checker error: %s", e)

    session_manager._stuck_checker_task = asyncio.create_task(_check_stuck_sessions())
