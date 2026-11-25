# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only diagnostics for organization turns that do not finish."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)
_STOP_TIMEOUT_SECONDS = 1.0


class OrganizationTurnDiagnostics:
    """Trace lifecycle boundaries and periodically sample Summary Team state."""

    def __init__(self, team_id: str, session_id: str, request_id: str, source: str) -> None:
        self.identity = (team_id, session_id, request_id, source)
        self.started_at = time.monotonic()
        self.last_chunk_at = self.started_at
        self.phase = "activate"
        self.chunk_count = 0
        self.task: asyncio.Task | None = None

    def mark(self, phase: str) -> None:
        self.phase = phase
        logger.info(
            "[OrgTurnDiagnostics] team=%s session=%s request=%s source=%s phase=%s elapsed_s=%.2f",
            *self.identity, phase, time.monotonic() - self.started_at,
        )

    def observe_chunk(self) -> None:
        self.chunk_count += 1
        self.last_chunk_at = time.monotonic()

    def start(self, agent: Any, summary_task_id: str | None) -> None:
        if self.identity[3] == "org_summary_background":
            owner = asyncio.current_task()
            self.task = asyncio.create_task(
                self._sample(agent, summary_task_id, owner), name="summary-turn-diagnostics",
            )

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        try:
            done, _ = await asyncio.wait({task}, timeout=_STOP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.add_done_callback(self._consume_result)
            raise
        if done:
            self._consume_result(task)
        else:
            logger.warning("Diagnostic sampler did not stop promptly: team=%s", self.identity[0])
            task.add_done_callback(self._consume_result)

    def _consume_result(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Diagnostic sampler failed: team=%s", self.identity[0], exc_info=True)

    async def _sample(self, agent: Any, summary_task_id: str | None, owner: asyncio.Task | None) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                registry = getattr(getattr(agent, "state", None), "member_registry", None)
                harness = getattr(agent, "harness", None)
                controller = getattr(agent, "_stream_controller", None)
                idle_timer = getattr(controller, "_idle_marker_task", None)
                stream_queue = getattr(controller, "stream_queue", None)
                snapshot = {
                    "member_registry": dict(list(registry.snapshot().items())[:100]) if registry else None,
                    "registry_idle": registry.is_idle() if registry else None,
                    "harness_state": getattr(getattr(harness, "state", None), "value", None),
                    "idle_timer": (
                        "absent" if idle_timer is None else "done" if idle_timer.done() else "pending"
                    ),
                    "stream_queue_size": stream_queue.qsize() if isinstance(stream_queue, asyncio.Queue) else None,
                }
                for name in ("is_agent_running", "has_in_flight_round", "has_pending_interrupt"):
                    probe = getattr(agent, name, None)
                    snapshot[name] = probe() if callable(probe) else None
                # Capture frame locations only; coroutine locals can contain prompts and credentials.
                awaiting = owner.get_coro() if owner is not None else None
                frames = []
                for _ in range(12):
                    if awaiting is None:
                        break
                    frame = getattr(awaiting, "cr_frame", None) or getattr(awaiting, "ag_frame", None)
                    if frame is not None:
                        frames.append(f"{frame.f_code.co_filename}:{frame.f_code.co_name}:{frame.f_lineno}")
                    awaiting = getattr(awaiting, "cr_await", None) or getattr(awaiting, "ag_await", None)
                snapshot["awaiting"] = frames
                logger.info(
                    "[OrgTurnDiagnostics] team=%s session=%s request=%s source=%s "
                    "phase=%s elapsed_s=%.2f last_chunk_age_s=%.2f chunks=%s runtime=%s",
                    *self.identity, self.phase, time.monotonic() - self.started_at,
                    time.monotonic() - self.last_chunk_at, self.chunk_count,
                    json.dumps(snapshot, ensure_ascii=False),
                )
                # Bound diagnostic reads independently; never time out or cancel the business turn.
                persisted = await asyncio.wait_for(self._read_persisted(agent, summary_task_id), timeout=3)
                logger.info(
                    "[OrgTurnDiagnostics] team=%s session=%s request=%s source=%s persisted=%s",
                    *self.identity, json.dumps(persisted, ensure_ascii=False),
                )
            except Exception:
                logger.warning(
                    "[OrgTurnDiagnostics] snapshot failed team=%s session=%s request=%s source=%s",
                    *self.identity, exc_info=True,
                )

    async def _read_persisted(self, agent: Any, summary_task_id: str | None) -> dict[str, Any]:
        backend = getattr(agent, "team_backend", None)
        db = getattr(backend, "db", None)
        result: dict[str, Any] = {}
        members = getattr(getattr(db, "member", None), "get_team_members", None)
        if callable(members):
            rows = await members(self.identity[0])
            result["members"] = [
                {"name": row.member_name, "status": str(row.status)} for row in rows[:100]
            ]
        count = getattr(getattr(db, "task", None), "count_tasks_terminality", None)
        if callable(count):
            total, non_terminal = await count(self.identity[0])
            result["tasks_total"] = total
            result["tasks_non_terminal"] = non_terminal
        manager = getattr(backend, "org_task_manager", None)
        get_task = getattr(manager, "get_task", None)
        if summary_task_id and callable(get_task):
            task = await get_task(summary_task_id)
            result["summary_task_id"] = summary_task_id
            result["summary_task_status"] = str(task.status) if task is not None else "missing"
        return result
