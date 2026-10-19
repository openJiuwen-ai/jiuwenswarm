# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dest-native subagent stream projection, batching, and parent-scoped persist.

Ported from develop persist/project helpers. Batches are keyed by parent
session; the lock never surrounds await. Dest ``task.*`` / Skill Turbo
``stream_source_id`` stay on their existing paths.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    from openjiuwen.harness.subagent_runtime import (
        SUBAGENT_ACTIVITY_EVENT_TYPE,
        SUBAGENT_MESSAGE_EVENT_TYPE,
        SUBAGENT_UPDATED_EVENT_TYPE,
    )
except ImportError:  # official dest-stable SDK may not have 3A yet
    SUBAGENT_UPDATED_EVENT_TYPE = "subagent_updated"
    SUBAGENT_MESSAGE_EVENT_TYPE = "subagent_message"
    SUBAGENT_ACTIVITY_EVENT_TYPE = "subagent_activity"

_SUBAGENT_PROGRESS_BATCHES: dict[str, list[str]] = {}
_SUBAGENT_PROGRESS_BATCHES_LOCK = threading.Lock()


def clear_subagent_progress_batch(parent_session_id: str | None) -> None:
    """Drop the parallel-index batch for one parent session."""
    if not parent_session_id:
        return
    with _SUBAGENT_PROGRESS_BATCHES_LOCK:
        _SUBAGENT_PROGRESS_BATCHES.pop(parent_session_id, None)


def clear_all_subagent_progress_batches() -> None:
    """Test helper: reset every parent-session batch."""
    with _SUBAGENT_PROGRESS_BATCHES_LOCK:
        _SUBAGENT_PROGRESS_BATCHES.clear()


def resolve_subagent_parallel_fields(
    *,
    parent_session_id: str,
    subagent_id: str,
    legacy_status: str,
) -> tuple[int, int, bool]:
    """Assign stable 0-based index/total for legacy Web SubtaskProgress."""
    if not parent_session_id or not subagent_id:
        return 0, 1, False

    with _SUBAGENT_PROGRESS_BATCHES_LOCK:
        order = _SUBAGENT_PROGRESS_BATCHES.setdefault(parent_session_id, [])

        if legacy_status in ("completed", "error"):
            if subagent_id not in order:
                return 0, 1, False
            index = order.index(subagent_id)
            total = max(len(order), 1)
            order.remove(subagent_id)
            if not order:
                _SUBAGENT_PROGRESS_BATCHES.pop(parent_session_id, None)
            return index, total, total > 1

        if subagent_id not in order:
            order.append(subagent_id)
        index = order.index(subagent_id)
        total = len(order)
        return index, total, total > 1


def resolve_subagent_legacy_status(projection: dict[str, Any]) -> tuple[str, str]:
    """Return (legacy_status, message) for Web SubtaskProgress compatibility."""
    status = str(projection.get("status") or "running")
    closed_reason = projection.get("closed_reason")
    message = ""
    if status == "closed":
        if closed_reason == "failed":
            legacy_status = "error"
            error = projection.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
        else:
            legacy_status = "completed"
    elif status == "idle":
        turn_outcome = projection.get("turn_outcome")
        if turn_outcome == "failed":
            legacy_status = "error"
            error = projection.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
        else:
            legacy_status = "completed"
    else:
        legacy_status = "starting"
    return legacy_status, message


def project_subagent_updated_for_web(projection: dict[str, Any]) -> dict[str, Any]:
    """Project runtime subagent_updated for Web without overwriting canonical status."""
    subagent_id = str(projection.get("subagent_id") or "")
    description = (
        str(projection.get("display_name") or "").strip()
        or str(projection.get("task_description") or "").strip()
        or subagent_id
    )
    legacy_status, message = resolve_subagent_legacy_status(projection)

    parent_session_id = str(projection.get("parent_session_id") or "")
    index, total, is_parallel = resolve_subagent_parallel_fields(
        parent_session_id=parent_session_id,
        subagent_id=subagent_id,
        legacy_status=legacy_status,
    )

    payload: dict[str, Any] = {
        "event_type": "chat.subtask_update",
        **projection,
        "session_id": parent_session_id,
        "task_id": subagent_id,
        "description": description,
        "legacy_status": legacy_status,
        "index": index,
        "total": total,
        "is_parallel": is_parallel,
    }
    if message:
        payload["message"] = message
    return payload


def persist_subagent_transcript_message(projection: dict[str, Any]) -> None:
    """Write a child transcript row under the parent session, not the parent jsonl."""
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return

    seq = projection.get("seq")
    request_id = f"{subagent_id}:{seq}" if seq is not None else subagent_id
    role = str(projection.get("role") or "assistant")
    event_type = str(projection.get("event_type") or "").strip() or None
    content = str(projection.get("content") or "")
    timestamp_ms = projection.get("at_ms")
    timestamp = float(timestamp_ms) / 1000 if timestamp_ms else time.time()

    extra: dict[str, Any] = {}
    reasoning_content = projection.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        extra["reasoning_content"] = reasoning_content.strip()
    try:
        phase_id = int(projection.get("phase_id") or 0)
    except (TypeError, ValueError):
        phase_id = 0
    if phase_id > 0:
        extra["phase_id"] = phase_id
    nested_extra = projection.get("extra")
    if isinstance(nested_extra, dict):
        extra.update(nested_extra)
    extra["parent_session_id"] = parent_session_id

    append_history_record(
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=request_id,
        channel_id="subagent",
        role=role,
        content=content,
        timestamp=timestamp,
        event_type=event_type if role == "assistant" else None,
        extra=extra or None,
        mode="subagent",
    )


def persist_subagent_activity(projection: dict[str, Any]) -> None:
    """Write a child activity snapshot under the parent session."""
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return

    task_id = str(projection.get("task_id") or "").strip() or "turn"
    seq = projection.get("seq")
    seq_part = str(seq) if seq is not None else str(projection.get("at_ms") or "0")
    request_id = f"{subagent_id}:activity:{task_id}:{seq_part}"
    timestamp_ms = projection.get("at_ms")
    timestamp = float(timestamp_ms) / 1000 if timestamp_ms else time.time()
    activity = {**projection, "parent_session_id": parent_session_id}
    append_history_record(
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=request_id,
        channel_id="subagent",
        role="assistant",
        content=str(projection.get("summary") or ""),
        timestamp=timestamp,
        event_type="chat.subagent_activity",
        extra={"subagent_activity": activity},
        mode="subagent",
    )


def persist_subagent_roster_history(
    projection: dict[str, Any],
    web_payload: dict[str, Any],
) -> None:
    """Write a child roster snapshot under the parent session."""
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return

    updated_at_ms = projection.get("updated_at_ms") or projection.get("created_at_ms")
    timestamp = float(updated_at_ms) / 1000 if updated_at_ms else time.time()
    revision = projection.get("revision")
    request_id = (
        f"subagent-roster-{subagent_id}:{revision}"
        if revision is not None
        else f"subagent-roster-{subagent_id}"
    )
    append_history_record(
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=request_id,
        channel_id="subagent",
        role="assistant",
        event_type="chat.subtask_update",
        content=str(web_payload.get("description") or subagent_id),
        timestamp=timestamp,
        extra=web_payload,
        mode="subagent",
    )


def _safe_persist(label: str, fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:
        logger.warning("persist %s failed", label, exc_info=True)


def try_handle_subagent_chunk(
    chunk_type: Any,
    payload: Any,
    *,
    parent_session_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Return (handled, web_payload). Message frames persist only (no web emit)."""
    if chunk_type == SUBAGENT_UPDATED_EVENT_TYPE:
        projection = payload.get("subagent_updated") if isinstance(payload, dict) else None
        if not isinstance(projection, dict):
            return True, None
        web_payload = project_subagent_updated_for_web(projection)
        _safe_persist(
            "subagent roster",
            persist_subagent_roster_history,
            projection,
            web_payload,
        )
        return True, web_payload

    if chunk_type == SUBAGENT_MESSAGE_EVENT_TYPE:
        projection = payload.get("subagent_message") if isinstance(payload, dict) else None
        if not isinstance(projection, dict):
            return True, None
        _safe_persist("subagent transcript", persist_subagent_transcript_message, projection)
        return True, None

    if chunk_type == SUBAGENT_ACTIVITY_EVENT_TYPE:
        projection = payload.get("subagent_activity") if isinstance(payload, dict) else None
        if not isinstance(projection, dict):
            return True, None
        persist_projection = dict(projection)
        resolved_parent = str(
            persist_projection.get("parent_session_id") or parent_session_id or ""
        ).strip()
        if resolved_parent:
            persist_projection["parent_session_id"] = resolved_parent
        _safe_persist("subagent activity", persist_subagent_activity, persist_projection)
        activity_payload: dict[str, Any] = {
            "event_type": "chat.subagent_activity",
            **projection,
        }
        if resolved_parent:
            activity_payload["session_id"] = resolved_parent
            activity_payload["parent_session_id"] = resolved_parent
        return True, activity_payload

    return False, None
