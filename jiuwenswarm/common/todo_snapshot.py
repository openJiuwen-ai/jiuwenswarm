# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Todo workspace snapshot helpers for frontend restore.

Reads ``~/.jiuwenswarm/agent/workspace/todo/{session_id}/todo.json`` without
requiring a live DeepAgent, and formats items the same way as
``JiuSwarmStreamEventRail._format_todos_for_frontend``.
"""

from __future__ import annotations

import json
from typing import Any

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.utils import get_deepagent_todo_dir

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

_CANCELLED_STATUSES = frozenset(
    {
        TodoStatus.CANCELLED,
        "cancelled",
        "canceled",
        "deleted",
    }
)


def format_todos_for_frontend(todos_data: list[Any]) -> list[dict[str, Any]]:
    """Format todo items for frontend ``todo.updated`` payload.

    Accepts OpenJiuWen ``TodoItem`` objects or raw ``todo.json`` dicts.
    Cancelled items are omitted; completed items are kept.
    """
    formatted: list[dict[str, Any]] = []
    for item in todos_data:
        if item is None:
            continue

        if isinstance(item, dict):
            status_raw = item.get("status", "pending")
            if status_raw in _CANCELLED_STATUSES:
                continue
            status_key = status_raw.value if hasattr(status_raw, "value") else str(status_raw).lower()
            if status_key in ("cancelled", "canceled", "deleted"):
                continue
            todo_id = item.get("id")
            if todo_id is None or todo_id == "":
                continue
            content = item.get("content") if isinstance(item.get("content"), str) else ""
            active_form = item.get("activeForm")
            if not isinstance(active_form, str):
                active_form = content
            formatted.append(
                {
                    "id": str(todo_id),
                    "content": content,
                    "activeForm": active_form,
                    "status": _STATUS_TO_FRONTEND.get(status_raw, _STATUS_TO_FRONTEND.get(status_key, "pending")),
                }
            )
            continue

        status = getattr(item, "status", None)
        if status in _CANCELLED_STATUSES:
            continue
        status_value = getattr(status, "value", None)
        if isinstance(status_value, str) and status_value.lower() in ("cancelled", "canceled", "deleted"):
            continue

        todo_id = getattr(item, "id", None)
        if todo_id is None or todo_id == "":
            continue
        content = getattr(item, "content", "") or ""
        active_form = getattr(item, "activeForm", None)
        if not isinstance(active_form, str):
            active_form = content if isinstance(content, str) else ""

        formatted.append(
            {
                "id": str(todo_id),
                "content": content if isinstance(content, str) else "",
                "activeForm": active_form,
                "status": _STATUS_TO_FRONTEND.get(status, status_value or "pending"),
            }
        )
    return formatted


def load_todo_snapshot_for_frontend(session_id: str) -> list[dict[str, Any]]:
    """Load and format the session todo.json snapshot.

    Missing / unreadable / empty files return ``[]`` so callers can explicitly
    clear the frontend panel on session restore.
    """
    sid = (session_id or "").strip()
    if not sid:
        return []

    path = get_deepagent_todo_dir() / sid / "todo.json"
    if not path.is_file():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(raw, list):
        return []
    return format_todos_for_frontend(raw)
