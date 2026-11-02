# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and format persisted Todo snapshots for frontend restoration."""

from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.todo_generation import (
    filter_todos_by_generation,
)
from jiuwenswarm.common.utils import get_deepagent_todo_dir

# Output policy is fixed by the host, never by the model or client request.
TODO_SNAPSHOT_MODE = ContextVar("todo_snapshot_mode", default="legacy")


def versioned_todo_output() -> bool:
    return TODO_SNAPSHOT_MODE.get() == "versioned"


def resolve_todo_snapshot_mode(config: dict, app_id: str | None) -> str:
    if not app_id:
        return "legacy"
    mode = config.get("todo", {}).get("snapshot_modes", {}).get(app_id, "legacy")
    if mode not in ("legacy", "versioned"):
        raise ValueError("Todo snapshot mode must be legacy or versioned")
    return mode


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


class FrontendTodos(list):
    """Existing list interface with metadata from the same file read."""
    def __init__(self, items, meta):
        super().__init__(items)
        self.todo_meta = meta


def project_todo_snapshot(snapshot, generation_token=None) -> FrontendTodos:
    """同一次提交按接入方展示，旧协议不泄漏内部版本。"""
    versioned = versioned_todo_output()
    items = snapshot.todos if versioned else filter_todos_by_generation(snapshot.todos, generation_token)
    todos = format_todos_for_frontend(items)
    if not versioned:
        todos = overlay_serial_todo_statuses(todos)
    meta = {"plan_id": snapshot.plan_id, "revision": snapshot.revision} if versioned else None
    return FrontendTodos(todos, meta)


def load_todo_snapshot_for_frontend(
    session_id: str, *, todo_root: Path | None = None, generation_token: str | None = None,
) -> FrontendTodos | None:
    """Return a complete snapshot; unavailable input must not clear the UI."""
    normalized = (session_id or "").strip()
    if not normalized or _SAFE_SESSION_ID.fullmatch(normalized) is None:
        return None
    root = todo_root if todo_root is not None else get_deepagent_todo_dir()
    path = root / normalized / "todo.json"
    from openjiuwen.harness.tools.todo_store import decode_snapshot

    try:
        snapshot = decode_snapshot(path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None
    return project_todo_snapshot(snapshot, generation_token)
