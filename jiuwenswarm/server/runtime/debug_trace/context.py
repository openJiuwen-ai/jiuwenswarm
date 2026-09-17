# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-wide ContextVar bridging a run's :class:`DebugTraceLogger` to the
subagent dispatch sites (``TaskTool`` in the SDK, ``AgentTool`` in jiuwenswarm).

The logger is created per-run as a local in
``interface_deep.JiuWenSwarmDeepAdapter._run_agent_streaming_internal`` and is
otherwise unreachable from the dispatch code paths. A ContextVar lets the
adapter publish it for the duration of a run; ``invoke_subagent_with_trace``
reads it (never writes) to decide whether to capture the subagent's stream.

``asyncio.create_task`` copies the current ContextVar snapshot, so background
subagents (``AgentTool`` ``background=True``) inherit the active logger too.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.debug_trace.stream_logger import DebugTraceLogger

_DEBUG_TRACE_LOGGER: ContextVar[Optional["DebugTraceLogger"]] = ContextVar(
    "debug_trace_logger",
    default=None,
)

# session_id -> logger registry. The per-request ContextVar can't reach the
# subagent dispatch sites: they run in the DeepAgent supervisor task created at
# session setup, before any /debug request. They hold the
# parent Session, so they look the logger up by id here.
_LOGGERS_BY_SESSION: dict[str, "DebugTraceLogger"] = {}


def get_debug_trace_logger() -> Optional["DebugTraceLogger"]:
    """Return the active run's logger, or ``None`` when not in a debug run."""
    return _DEBUG_TRACE_LOGGER.get()


def set_debug_trace_logger(logger: Optional["DebugTraceLogger"]) -> Token:
    """Publish *logger* as the active run's logger; returns a reset token."""
    return _DEBUG_TRACE_LOGGER.set(logger)


def reset_debug_trace_logger(token: Token) -> None:
    """Restore the previous logger binding using the token from :func:`set`."""
    _DEBUG_TRACE_LOGGER.reset(token)


def register_debug_trace_logger(session_id: str, logger: "DebugTraceLogger") -> None:
    """Register *logger* as the active logger for *session_id*.

    Pairs with :func:`unregister_debug_trace_logger`; the adapter calls these at
    run start / end so dispatch sites running outside the request's ContextVar
    scope (the DeepAgent supervisor task) can still recover the logger via
    :func:`get_debug_trace_logger_for_session`.
    """
    if session_id:
        _LOGGERS_BY_SESSION[session_id] = logger


def unregister_debug_trace_logger(session_id: str) -> None:
    """Drop the logger registered for *session_id* (no-op if none / id empty)."""
    if session_id:
        _LOGGERS_BY_SESSION.pop(session_id, None)


def get_debug_trace_logger_for_session(session_id: str) -> Optional["DebugTraceLogger"]:
    """Return the logger registered for *session_id*, or ``None``.

    Fallback for dispatch sites that run in a task whose context snapshot predates
    the per-request ContextVar binding (see ``_LOGGERS_BY_SESSION`` note).
    """
    return _LOGGERS_BY_SESSION.get(session_id) if session_id else None


__all__ = [
    "get_debug_trace_logger",
    "set_debug_trace_logger",
    "reset_debug_trace_logger",
    "register_debug_trace_logger",
    "unregister_debug_trace_logger",
    "get_debug_trace_logger_for_session",
]
