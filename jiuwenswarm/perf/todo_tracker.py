# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Track main-agent todo transitions into request_summaries ``tasks``.

Source of truth: parent ``StreamEventRail`` (main workspace todo list).
Team-member / subagent todo lists are never synced here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jiuwenswarm.perf.config import get_perf_summary_config
from jiuwenswarm.perf.context import get_request_context, set_active_task_id
from jiuwenswarm.perf.task_hooks import notify_task_complete

logger = logging.getLogger(__name__)

_DONE = frozenset({"completed", "cancelled", "canceled"})
_ACTIVE = frozenset({"in_progress"})
_STALE_AGE_S = 7200.0

_LOCK = threading.Lock()
# request_id -> todo_id -> state
_STATES: dict[str, dict[str, "_TodoState"]] = {}
_CREATED_AT: dict[str, float] = {}


@dataclass
class _TodoState:
    content: str
    status: str
    started_at: float | None = None
    recorded: bool = False


@dataclass(frozen=True)
class _PendingRecord:
    todo_id: str
    content: str
    status: str
    started_at: float
    ended_at: float


def sync_main_agent_todos(
    todos: Sequence[Mapping[str, Any]] | None,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
    now: float | None = None,
) -> None:
    """Diff a main-agent todo snapshot and record newly finished items."""
    cfg = get_perf_summary_config()
    if not cfg.enabled or not cfg.include_tasks:
        return

    rid = (request_id or "").strip()
    if not rid:
        req_ctx = get_request_context(session_id=session_id)
        if req_ctx is None:
            logger.debug(
                "[perf] skip todo sync: no request context session_id=%s",
                session_id,
            )
            return
        rid = str(req_ctx.get("request_id") or "").strip()
    if not rid:
        logger.debug(
            "[perf] skip todo sync: empty request_id session_id=%s",
            session_id,
        )
        return

    ts = float(now if now is not None else time.time())
    pending: list[_PendingRecord] = []

    with _LOCK:
        _prune_stale_locked(ts)
        states = _STATES.setdefault(rid, {})
        _CREATED_AT.setdefault(rid, ts)
        for raw in todos or []:
            todo_id = str(raw.get("id") or "").strip()
            if not todo_id:
                continue
            content = str(raw.get("content") or "")
            status = str(raw.get("status") or "pending").strip().lower()
            prev = states.get(todo_id)

            if prev is None:
                # First sight anchors duration when agents skip in_progress.
                state = _TodoState(
                    content=content,
                    status=status,
                    started_at=ts,
                )
                states[todo_id] = state
                if status in _DONE and not state.recorded:
                    state.recorded = True
                    pending.append(
                        _PendingRecord(
                            todo_id=todo_id,
                            content=content,
                            status=status,
                            started_at=ts,
                            ended_at=ts,
                        )
                    )
                continue

            if content:
                prev.content = content

            if status in _ACTIVE and prev.status not in _ACTIVE:
                prev.started_at = ts

            if status in _DONE and prev.status not in _DONE and not prev.recorded:
                started_at = prev.started_at if prev.started_at is not None else ts
                prev.recorded = True
                pending.append(
                    _PendingRecord(
                        todo_id=todo_id,
                        content=prev.content or content,
                        status=status,
                        started_at=started_at,
                        ended_at=ts,
                    )
                )

            prev.status = status

    # Bind the first in_progress main todo so llm/tool hooks can attribute
    # without enabling TaskExecutionRail (session-registry / shared ctx dict).
    active_task_id = None
    for raw in todos or []:
        todo_id = str(raw.get("id") or "").strip()
        if not todo_id:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status in _ACTIVE:
            active_task_id = f"todo:{todo_id}"
            break
    set_active_task_id(
        active_task_id,
        session_id=session_id,
        request_id=rid,
    )

    for item in pending:
        _emit_record(rid, item)


def clear_todo_tracker(request_id: str | None) -> None:
    """Drop per-request todo tracking state."""
    rid = (request_id or "").strip()
    if not rid:
        return
    with _LOCK:
        _STATES.pop(rid, None)
        _CREATED_AT.pop(rid, None)
    set_active_task_id(None, request_id=rid)


def prune_stale_todo_trackers(*, now: float | None = None) -> int:
    """Drop abandoned request trackers older than ``_STALE_AGE_S``."""
    ts = float(now if now is not None else time.time())
    with _LOCK:
        return _prune_stale_locked(ts)


def _prune_stale_locked(now: float) -> int:
    stale = [
        rid
        for rid, created in _CREATED_AT.items()
        if now - created > _STALE_AGE_S
    ]
    for rid in stale:
        _STATES.pop(rid, None)
        _CREATED_AT.pop(rid, None)
    return len(stale)


def _emit_record(request_id: str, item: _PendingRecord) -> None:
    duration_ms = max(0.0, (item.ended_at - item.started_at) * 1000.0)
    perf_status = (
        "failed" if item.status in ("cancelled", "canceled") else "succeeded"
    )
    notify_task_complete(
        task_id=f"todo:{item.todo_id}",
        task_content=item.content,
        source="todo",
        started_at=item.started_at,
        ended_at=item.ended_at,
        duration_ms=duration_ms,
        status=perf_status,
        request_id=request_id,
    )


def todos_from_items(todos_data: Sequence[Any]) -> list[dict[str, str]]:
    """Normalize TodoItem objects / dicts into tracker snapshots."""
    out: list[dict[str, str]] = []
    for item in todos_data or []:
        if isinstance(item, Mapping):
            todo_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "")
            status_raw = item.get("status", "pending")
        else:
            todo_id = str(getattr(item, "id", "") or "").strip()
            content = str(getattr(item, "content", "") or "")
            status_raw = getattr(item, "status", "pending")
        if not todo_id:
            continue
        if hasattr(status_raw, "value"):
            status = str(status_raw.value)
        else:
            status = str(status_raw or "pending")
        out.append({"id": todo_id, "content": content, "status": status.lower()})
    return out
