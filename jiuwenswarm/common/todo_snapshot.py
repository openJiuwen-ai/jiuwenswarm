# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and format persisted Todo snapshots for frontend restoration."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.todo_generation import (
    filter_todos_by_generation,
)
from jiuwenswarm.common.todo_protocol import (
    decode_todo_document,
    todo_event_metadata,
    todo_v2_enabled,
)
from jiuwenswarm.common.utils import get_deepagent_todo_dir

_SAFE_SESSION_ID = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$"
)

_STATUS_TO_FRONTEND = {
    TodoStatus.PENDING: "pending",
    TodoStatus.IN_PROGRESS: "in_progress",
    TodoStatus.COMPLETED: "completed",
    "pending": "pending",
    "waiting": "pending",
    "in_progress": "in_progress",
    "running": "in_progress",
    "completed": "completed",
}
_CANCELLED_STATUSES = frozenset({
    TodoStatus.CANCELLED,
    "cancelled",
    "canceled",
    "deleted",
})
_SERIAL_TODO_DONE_STATUSES = frozenset({"completed", "cancelled"})


def overlay_serial_todo_statuses(
    items: list[dict[str, Any]],
    *,
    done_statuses: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Hide out-of-order completed/in-progress rows from a UI snapshot."""
    done = done_statuses if done_statuses is not None else _SERIAL_TODO_DONE_STATUSES
    overlay: list[dict[str, Any]] = []
    found_open = False
    for item in items:
        copied = dict(item)
        status = str(copied.get("status", "pending")).lower()
        if status in done:
            if found_open and status == "completed":
                copied["status"] = "pending"
        elif found_open:
            copied["status"] = "pending"
        else:
            found_open = True
        overlay.append(copied)
    return overlay


def format_todos_for_frontend(todos_data: list[Any]) -> list[dict[str, Any]]:
    """Convert Todo objects or persisted dictionaries to frontend fields."""
    formatted: list[dict[str, Any]] = []
    for item in todos_data:
        if item is None:
            continue
        if isinstance(item, dict):
            status = item.get("status", "pending")
            todo_id = item.get("id")
            content = item.get("content")
            active_form = item.get("activeForm")
        else:
            status = getattr(item, "status", None)
            todo_id = getattr(item, "id", None)
            content = getattr(item, "content", "")
            active_form = getattr(item, "activeForm", None)

        status_value = getattr(status, "value", status)
        status_key = (
            status_value.lower()
            if isinstance(status_value, str) and status_value
            else "pending"
        )
        if status_key in _CANCELLED_STATUSES:
            continue
        if todo_id is None or todo_id == "":
            continue
        content_text = content if isinstance(content, str) else ""
        formatted.append({
            "id": str(todo_id),
            "content": content_text,
            "activeForm": active_form if isinstance(active_form, str) else content_text,
            "status": _STATUS_TO_FRONTEND.get(status_key, "pending"),
        })
    return formatted


def load_todo_snapshot_for_frontend(
    session_id: str,
    *,
    todo_root: Path | None = None,
    generation_token: str | None = None,
) -> list[dict[str, Any]]:
    """Read one session's Todo snapshot; invalid or unreadable input is empty."""
    payload = load_todo_event_for_frontend(session_id, todo_root=todo_root, generation_token=generation_token)
    return payload["todos"] if payload is not None else []


def todo_snapshot_payload(items: list[Any], metadata: dict | None, generation_token: str | None = None) -> dict:
    if metadata is None:
        visible = filter_todos_by_generation(items, generation_token)
        return {"todos": overlay_serial_todo_statuses(format_todos_for_frontend(visible))}
    token = metadata.get("generation_token")
    visible = []
    for item in items:
        data = item if isinstance(item, dict) else item.to_dict()
        if (data.get("meta_data") or {}).get("persistent") or not token or data.get("generation_token") in (None, token):
            visible.append(item)
    return {"todos": format_todos_for_frontend(visible), "todo_meta": todo_event_metadata(metadata)}


def load_todo_event_for_frontend(
    session_id: str, *, todo_root: Path | None = None, generation_token: str | None = None,
) -> dict | None:
    """Return a complete event, distinguishing read failure from an empty plan."""
    normalized = (session_id or "").strip()
    if not normalized or _SAFE_SESSION_ID.fullmatch(normalized) is None:
        return None
    root = todo_root if todo_root is not None else get_deepagent_todo_dir()
    path = root / normalized / "todo.json"
    if not path.is_file():
        return None if todo_v2_enabled() else {"todos": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items, metadata = decode_todo_document(raw)
        return todo_snapshot_payload(items, metadata, generation_token)
    except (OSError, UnicodeError, ValueError):
        return None


async def publish_todo_receipt(session: Any, receipt: Any) -> bool:
    """Publish a committed v2 snapshot outside the storage transaction."""
    snapshot = getattr(receipt, "snapshot", None)
    metadata = getattr(snapshot, "metadata", None)
    if not isinstance(metadata, dict) or session is None:
        return False
    return await publish_todo_payload(session, todo_snapshot_payload(snapshot.todos, metadata))


async def publish_todo_payload(session: Any, payload: dict) -> bool:
    from openjiuwen.core.session.stream import OutputSchema

    delivered = False
    for _ in range(3):
        try:
            await session.write_stream(OutputSchema(type="todo.updated", index=0, payload=payload))
            delivered = True
            break
        except Exception:  # 通知失败不回滚提交；下一轮/历史恢复重读权威快照。
            logging.getLogger(__name__).warning("Todo snapshot enqueue failed", exc_info=True)
    update = getattr(session, "update_state", None)
    if callable(update):
        update({"todo_snapshot_resync_pending": not delivered})
    return delivered
