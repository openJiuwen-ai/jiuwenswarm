"""Track synchronous CUA subagents owned by one managed task execution."""

from __future__ import annotations

import asyncio

# Core session -> managed request running in it.
_SESSION_REQUESTS: dict[str, str] = {}
# Managed request -> live CUA delegation tasks (one request may run several).
_CUA_TASKS: dict[str, list[asyncio.Task]] = {}


def bind_managed_task_session(session_id: str, request_id: str) -> tuple[str, str]:
    """Bind a Core Agent session to the managed request it executes."""
    binding = (str(session_id or "").strip(), str(request_id or "").strip())
    if all(binding):
        _SESSION_REQUESTS[binding[0]] = binding[1]
    return binding


def unbind_managed_task_session(binding: tuple[str, str]) -> None:
    """Remove one session binding without deleting a newer execution."""
    if _SESSION_REQUESTS.get(binding[0]) == binding[1]:
        _SESSION_REQUESTS.pop(binding[0], None)


def register_cua_execution(*, parent_session_id: str, sub_session_id: str) -> tuple[str, asyncio.Task] | None:
    """Register the current CUA delegation when it belongs to a managed task."""
    request_id = _SESSION_REQUESTS.get(parent_session_id, "")
    task = asyncio.current_task()
    if not request_id or task is None:
        return None
    _CUA_TASKS.setdefault(request_id, []).append(task)
    return request_id, task


def unregister_cua_execution(registration: tuple[str, asyncio.Task] | None) -> None:
    """Remove exactly this registration and keep the other ones."""
    if registration is None:
        return
    request_id, task = registration
    remaining = [item for item in _CUA_TASKS.get(request_id, []) if item is not task]
    if remaining:
        _CUA_TASKS[request_id] = remaining
    else:
        _CUA_TASKS.pop(request_id, None)


def _live_cua_tasks(request_id: str) -> list[asyncio.Task]:
    request_id = str(request_id or "").strip()
    live = [task for task in _CUA_TASKS.get(request_id, []) if not task.done()]
    if live:
        _CUA_TASKS[request_id] = live
    else:
        _CUA_TASKS.pop(request_id, None)
    return live


def cancel_cua_execution(request_id: str) -> bool:
    """Cancel every active CUA delegation for a managed request."""
    tasks = _live_cua_tasks(request_id)
    for task in tasks:
        task.cancel()
    return bool(tasks)


def has_active_cua(request_id: str) -> bool:
    """Return whether a managed request still has a live CUA delegation."""
    return bool(_live_cua_tasks(request_id))
