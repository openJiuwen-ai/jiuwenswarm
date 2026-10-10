# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and format persisted Todo snapshots for frontend restoration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.utils import get_deepagent_todo_dir
from jiuwenswarm.common.todo_generation import (
    filter_todos_by_generation,
)

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
    normalized = (session_id or "").strip()
    if not normalized or _SAFE_SESSION_ID.fullmatch(normalized) is None:
        return []
    root = todo_root if todo_root is not None else get_deepagent_todo_dir()
    path = root / normalized / "todo.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    current_items = filter_todos_by_generation(raw, generation_token)
    return overlay_serial_todo_statuses(format_todos_for_frontend(current_items))
