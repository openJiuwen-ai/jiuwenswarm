"""Track synchronous subagents owned by one managed task execution."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass


_MANAGED_TASK_REQUEST: ContextVar[str] = ContextVar(
    "managed_task_request",
    default="",
)


@dataclass(frozen=True)
class ActiveCuaExecution:
    """The exact synchronous CUA delegation running for a managed task."""

    request_id: str
    parent_session_id: str
    sub_session_id: str
    task: asyncio.Task


@dataclass(frozen=True)
class ManagedTaskSession:
    """A managed request currently executing in one Core Agent session."""

    session_id: str
    request_id: str


_ACTIVE_CUA_EXECUTIONS: dict[str, ActiveCuaExecution] = {}
_MANAGED_TASK_SESSIONS: dict[str, ManagedTaskSession] = {}


def bind_managed_task_request(request_id: str) -> Token:
    """Bind a managed request to child asyncio tasks created by the agent."""
    return _MANAGED_TASK_REQUEST.set(str(request_id or "").strip())


def reset_managed_task_request(token: Token) -> None:
    """Restore the preceding managed-task context."""
    _MANAGED_TASK_REQUEST.reset(token)


def bind_managed_task_session(
    session_id: str,
    request_id: str,
) -> ManagedTaskSession:
    """Bind a Core Agent session without relying on ContextVar propagation."""
    binding = ManagedTaskSession(
        session_id=str(session_id or "").strip(),
        request_id=str(request_id or "").strip(),
    )
    if binding.session_id and binding.request_id:
        _MANAGED_TASK_SESSIONS[binding.session_id] = binding
    return binding


def unbind_managed_task_session(binding: ManagedTaskSession) -> None:
    """Remove one session binding without deleting a newer execution."""
    if _MANAGED_TASK_SESSIONS.get(binding.session_id) is binding:
        _MANAGED_TASK_SESSIONS.pop(binding.session_id, None)


def register_cua_execution(
    *,
    parent_session_id: str,
    sub_session_id: str,
) -> ActiveCuaExecution | None:
    """Register a CUA delegation only when it belongs to a managed task."""
    session_binding = _MANAGED_TASK_SESSIONS.get(parent_session_id)
    request_id = (
        session_binding.request_id
        if session_binding is not None
        else _MANAGED_TASK_REQUEST.get()
    )
    task = asyncio.current_task()
    if not request_id or task is None:
        return None
    execution = ActiveCuaExecution(
        request_id=request_id,
        parent_session_id=parent_session_id,
        sub_session_id=sub_session_id,
        task=task,
    )
    _ACTIVE_CUA_EXECUTIONS[request_id] = execution
    return execution


def unregister_cua_execution(execution: ActiveCuaExecution | None) -> None:
    """Remove one registration without deleting a newer replacement."""
    if execution is None:
        return
    if _ACTIVE_CUA_EXECUTIONS.get(execution.request_id) is execution:
        _ACTIVE_CUA_EXECUTIONS.pop(execution.request_id, None)


def cancel_cua_execution(request_id: str) -> bool:
    """Cancel the exact active CUA delegation for a managed request."""
    execution = _ACTIVE_CUA_EXECUTIONS.get(str(request_id or "").strip())
    if execution is None or execution.task.done():
        return False
    execution.task.cancel()
    return True


def active_cua_execution(request_id: str) -> ActiveCuaExecution | None:
    """Return the live CUA execution for diagnostics and tests."""
    execution = _ACTIVE_CUA_EXECUTIONS.get(str(request_id or "").strip())
    if execution is not None and execution.task.done():
        unregister_cua_execution(execution)
        return None
    return execution
