# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Todo workspace snapshot helpers for frontend restore.

Reads the session ``todo.json`` without requiring a live DeepAgent, and
formats items the same way as
``JiuSwarmStreamEventRail._format_todos_for_frontend``.

Write location follows DeepAgent ``WorkspaceNode.TODO``:

- Single-agent **code** with a locked ``project_dir`` writes
  ``{project_dir}/todo/{session_id}/todo.json``.
- Work mode, code without a project, and team members keep the default
  ``~/.jiuwenswarm/agent/workspace/todo/{session_id}/todo.json`` (team
  members additionally use their own workspace; the session restore
  panel is driven by ``team.task``, not this file).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.mode_matrix import is_code_profile_mode, is_team_mode, normalize_work_mode
from jiuwenswarm.common.utils import get_deepagent_todo_dir

_STATUS_TO_FRONTEND = {
    TodoStatus.PENDING: "pending",
    TodoStatus.IN_PROGRESS: "in_progress",
    TodoStatus.COMPLETED: "completed",
    TodoStatus.CANCELLED: "cancelled",
    "pending": "pending",
    "waiting": "pending",
    "in_progress": "in_progress",
    "running": "in_progress",
    "completed": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

# Items removed via todo ``delete`` are gone from the workspace by design;
# ``cancel`` keeps the item so the progress view can file it under "cancelled".
_DELETED_STATUSES = frozenset({"deleted"})


def format_todos_for_frontend(todos_data: list[Any]) -> list[dict[str, Any]]:
    """Format todo items for frontend ``todo.updated`` payload.

    Accepts OpenJiuWen ``TodoItem`` objects or raw ``todo.json`` dicts.
    Cancelled items are kept with status ``cancelled``; deleted items are
    omitted.
    """
    formatted: list[dict[str, Any]] = []
    for item in todos_data:
        if item is None:
            continue

        if isinstance(item, dict):
            status_raw = item.get("status", "pending")
            status_key = status_raw.value if hasattr(status_raw, "value") else str(status_raw).lower()
            if status_key in _DELETED_STATUSES:
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
        status_value = getattr(status, "value", None)
        if isinstance(status_value, str) and status_value.lower() in _DELETED_STATUSES:
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


def session_uses_project_todo_dir(
    *,
    project_dir: str | None = None,
    work_mode: str | None = None,
    mode: str | None = None,
) -> bool:
    """Whether this session's harness todos live under ``{project_dir}/todo``.

    ``project_dir`` is locked on work, code, and team sessions alike. Only
    single-agent code actually roots DeepAgent workspace (and therefore
    ``todo.json``) at that directory. Work mode keeps the agent workspace;
    team members keep a per-member workspace and only move cwd.
    """
    if not str(project_dir or "").strip():
        return False
    if is_team_mode(mode):
        return False
    if normalize_work_mode(work_mode) == "code":
        return True
    return is_code_profile_mode(mode)


def todo_snapshot_candidate_paths(
    session_id: str,
    *,
    project_dir: str | None = None,
    work_mode: str | None = None,
    mode: str | None = None,
) -> list[Path]:
    """Return todo.json paths to try, most specific first."""
    sid = (session_id or "").strip()
    if not sid:
        return []

    candidates: list[Path] = []
    locked_project = str(project_dir or "").strip()
    if session_uses_project_todo_dir(
        project_dir=locked_project,
        work_mode=work_mode,
        mode=mode,
    ):
        candidates.append(Path(locked_project) / "todo" / sid / "todo.json")
    candidates.append(get_deepagent_todo_dir() / sid / "todo.json")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _load_todo_json(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    return format_todos_for_frontend(raw)


def load_todo_snapshot_for_frontend(
    session_id: str,
    *,
    project_dir: str | None = None,
    work_mode: str | None = None,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """Load and format the session todo.json snapshot.

    Missing / unreadable / empty files return ``[]`` so callers can explicitly
    clear the frontend panel on session restore.
    """
    sid = (session_id or "").strip()
    if not sid:
        return []

    for path in todo_snapshot_candidate_paths(
        sid,
        project_dir=project_dir,
        work_mode=work_mode,
        mode=mode,
    ):
        loaded = _load_todo_json(path)
        if loaded is not None:
            return loaded
    return []
