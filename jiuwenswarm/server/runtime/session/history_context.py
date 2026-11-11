# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted session facts for platform transcript events."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import logging
from pathlib import Path
from typing import Callable, Iterator

from openjiuwen.core.events import SessionContext

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.server.runtime.session.model_selection_store import (
    SESSION_MODEL_INFO_KEY,
    capture_runtime_model_info,
)
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

logger = logging.getLogger(__name__)
@dataclass(slots=True)
class _HistoryScope:
    session_id: str
    request_id: str | None
    context: SessionContext | None = None
    workspace_bound: bool = False
    active: bool = True


_RUNTIME: ContextVar[_HistoryScope | None] = ContextVar(
    "history_runtime_context", default=None,
)


@contextmanager
def history_context_scope(session_id: str, *, request_id: str | None = None) -> Iterator[None]:
    """Own a request-local context slot and release it on exit or cancellation."""
    existing = _RUNTIME.get()
    if (
        existing is not None and existing.active and existing.session_id == session_id
        and existing.request_id == request_id
    ):
        yield
        return
    # Producer tasks inherit the same request-owned holder. Replacing a
    # ContextVar value inside a child task would not reach the history consumer.
    scope = _HistoryScope(session_id, request_id)
    token = _RUNTIME.set(scope)
    try:
        yield
    finally:
        scope.active = False
        try:
            _RUNTIME.reset(token)
        except ValueError:
            # An async generator may be closed from a different task (for
            # example through ``asyncio.wait_for(stream.aclose())``).  Its
            # token belongs to the consumer task, but the shared scope has
            # already been invalidated above, so it must not remain usable.
            logger.debug("[SessionHistory] history context closed from another task")


def history_context_request(method: Callable) -> Callable:
    """Keep one context owner around a complete platform unary request."""
    @wraps(method)
    async def wrapped(adapter, request):
        with history_context_scope(request.session_id or "default", request_id=request.request_id):
            return await method(adapter, request)
    return wrapped


def history_context_stream(method: Callable) -> Callable:
    """Keep producer and consumer tasks within one platform request owner."""
    @wraps(method)
    async def wrapped(adapter, request):
        with history_context_scope(request.session_id or "default", request_id=request.request_id):
            async with aclosing(method(adapter, request)) as stream:
                async for chunk in stream:
                    yield chunk
    return wrapped


def capture_history_workspace(
    session_id: str | None,
    *,
    cwd: str | None,
    project_root: str | None,
) -> None:
    """Record directories only when the host actually binds the workspace."""
    bound = _RUNTIME.get()
    if bound is None or not bound.active or bound.session_id != session_id:
        return
    metadata = dict(bound.context.metadata) if bound.context is not None else {}
    if project_root is not None:
        metadata["project_root"] = project_root
    else:
        metadata.pop("project_root", None)
    bound.context = SessionContext(session_id, cwd=cwd, metadata=metadata)
    bound.workspace_bound = True


def capture_history_model(session_id: str | None, model: object) -> None:
    """Record an applied model without borrowing pre-bind process directories."""
    bound = _RUNTIME.get()
    if bound is None or not bound.active or bound.session_id != session_id:
        return
    context = bound.context or SessionContext(session_id)
    metadata = dict(context.metadata)
    model_info = capture_runtime_model_info(model)
    if model_info is not None:
        metadata[SESSION_MODEL_INFO_KEY] = model_info
    else:
        metadata.pop(SESSION_MODEL_INFO_KEY, None)
    bound.context = SessionContext(session_id, cwd=context.cwd, metadata=metadata)


def bound_history_context(session_id: str) -> SessionContext | None:
    """Return facts only for this task's matching native request."""
    bound = _RUNTIME.get()
    return bound.context if bound is not None and bound.active and bound.session_id == session_id else None


def _absolute_path(value: object) -> str | None:
    if isinstance(value, str) and value and Path(value).is_absolute():
        return value
    return None


def _saved_context(session_id: str) -> SessionContext:
    """Read host metadata without writeback, model activation, or cwd defaults."""
    try:
        saved = get_session_metadata(session_id, enable_writeback=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        # Missing observation facts must not replace lifecycle checks at commit.
        logger.debug("[SessionHistory] session observation facts unavailable")
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    channel = saved.get("channel_metadata")
    channel = channel if isinstance(channel, dict) else {}
    cwd = None
    for value in (channel.get("cwd"), saved.get("cwd"), saved.get("project_dir"), channel.get("project_dir")):
        path = _absolute_path(value)
        if path is not None:
            cwd = path
            break
    root = _absolute_path(saved.get("project_dir"))
    metadata = {"project_root": root} if root is not None else {}
    return SessionContext(session_id, cwd=cwd, metadata=metadata)


async def platform_history_context(session_id: str) -> SessionContext:
    """Bind available host facts for a platform write, never from its payload."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    context = bound_history_context(session_id)
    scope = _RUNTIME.get()
    if context is not None and scope.workspace_bound:
        return context
    saved = await asyncio.to_thread(_saved_context, session_id)
    context = bound_history_context(session_id)
    scope = _RUNTIME.get()
    if context is not None and scope.workspace_bound:
        return context
    if context is not None:
        return SessionContext(session_id, cwd=saved.cwd, metadata={**saved.metadata, **context.metadata})
    return saved
