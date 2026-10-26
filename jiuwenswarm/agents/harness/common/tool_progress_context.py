"""Task-local progress callback for long-running agent tools."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable

ToolProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_CURRENT_TOOL_PROGRESS: ContextVar[ToolProgressCallback | None] = ContextVar(
    "current_tool_progress",
    default=None,
)


def bind_tool_progress(callback: ToolProgressCallback) -> Token:
    return _CURRENT_TOOL_PROGRESS.set(callback)


def current_tool_progress() -> ToolProgressCallback | None:
    return _CURRENT_TOOL_PROGRESS.get()


def reset_tool_progress(token: Token | None) -> None:
    if token is not None:
        _CURRENT_TOOL_PROGRESS.reset(token)
