# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only session capabilities contributed during ``ops.build``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
    ExtensionEventBus,
    HandlerLevel,
    SessionContext,
)

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.server.runtime.session.model_selection_store import (
    SESSION_MODEL_INFO_KEY,
    get_session_model_info,
    sanitize_model_info,
)
from jiuwenswarm.server.runtime.session.session_repo import read_session_repo


def _require_session(ops: EventHandlerOps, capability: str) -> SessionContext:
    context = ops.session_context
    if context is None:
        raise RuntimeError(f"{capability} requires SessionContext")
    return context


def _absolute_path(value: object, capability: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{capability} requires a string path or None")
    if not value or not Path(value).is_absolute():
        raise ValueError(f"{capability} requires an absolute path")
    return value


async def session_cwd(ops: EventHandlerOps) -> str | None:
    """Return this invocation's execution directory, or None when unavailable.

    Raises:
        RuntimeError: No session is bound to the current Handler.
        TypeError: The bound directory is not a string or None.
        ValueError: The bound directory is not an absolute path.
    """
    context = _require_session(ops, "session.cwd")
    return _absolute_path(context.cwd, "session.cwd")


async def session_root(ops: EventHandlerOps) -> str | None:
    """Return this invocation's bound project root, or None without a project.

    Raises:
        RuntimeError: No session is bound to the current Handler.
        TypeError: The bound project root is not a string or None.
        ValueError: The bound project root is not an absolute path.
    """
    context = _require_session(ops, "session.root")
    return _absolute_path(context.metadata.get("project_root"), "session.root")


async def session_model(ops: EventHandlerOps) -> dict[str, object] | None:
    """Read bound runtime model facts, or this session's saved selection.

    Raises:
        RuntimeError: No session is bound to the current Handler.
        TypeError: A host model snapshot contains invalid field types.
        ValueError: A host snapshot or bound session ID is invalid.
        ModelSelectionError: Saved model metadata cannot be read or validated.
    """
    context = _require_session(ops, "session.model")
    if not is_valid_session_id(context.session_id):
        raise ValueError("invalid session_id")
    snapshot = context.metadata.get(SESSION_MODEL_INFO_KEY)
    if snapshot is not None:
        return sanitize_model_info(snapshot)
    return await asyncio.to_thread(get_session_model_info, context.session_id)


async def session_repo(ops: EventHandlerOps) -> dict[str, object] | None:
    """Read Git identity at the bound cwd, or None without a Git working tree.

    Raises:
        RuntimeError: No session is bound to the current Handler.
        TypeError: The bound directory is not a string or None.
        ValueError: The bound directory is not an absolute path.
        SessionRepoError: The workspace or Git is unavailable, times out, or
            reports an unexpected failure. Raw Git output is never returned.
    """
    context = _require_session(ops, "session.repo")
    cwd = _absolute_path(context.cwd, "session.repo")
    if cwd is None:
        return None
    return await asyncio.to_thread(read_session_repo, cwd)


async def session_ops_build(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> EventHandlerOpsBuilder:
    """Contribute the session namespace to the host's shared Ops catalog."""
    del ops
    builder = await next_handler(event)
    if not isinstance(builder, EventHandlerOpsBuilder):
        raise TypeError("ops.build must return EventHandlerOpsBuilder")
    builder.add_namespace("session")
    builder.add_method("session", "cwd", session_cwd)
    builder.add_method("session", "root", session_root)
    builder.add_method("session", "model", session_model)
    builder.add_method("session", "repo", session_repo)
    return builder


class SessionFileService(Protocol):
    """Host-owned file data provider; no UI or package lifecycle ownership."""

    async def files(self, session_id: str, expanded_paths: list[str], channel_id: str) -> dict[str, Any]: ...


def register_session_ops(
    bus: ExtensionEventBus,
    *,
    file_service: SessionFileService | None = None,
) -> None:
    """Register workspace capabilities before Registry initializes Ops once."""

    async def files(ops: EventHandlerOps, *, expanded_paths: list[str] | None = None) -> dict[str, Any]:
        context = _require_session(ops, "session.files")
        if file_service is None:
            return {"status": "unavailable", "treeData": []}
        return await file_service.files(
            context.session_id,
            [] if expanded_paths is None else expanded_paths,
            str(context.metadata.get("channel_id") or "web"),
        )

    async def build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        builder = await session_ops_build(event, ops, next_handler)
        builder.add_method("session", "files", files)
        return builder

    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE,
        build,
        {},
        HandlerLevel.BUILTIN,
    )
