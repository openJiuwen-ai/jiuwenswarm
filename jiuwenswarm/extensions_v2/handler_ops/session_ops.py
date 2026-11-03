# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound-session catalog: seven event proxies plus existing append/file Ops.

The older direct reader functions remain storage-level compatibility helpers;
they are not the methods registered in the public Handler Ops catalog.
"""

from __future__ import annotations

import asyncio
from functools import partial
from collections.abc import Mapping
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
from jiuwenswarm.extensions_v2.event_types.session_queries import SESSION_QUERY_EVENTS
from jiuwenswarm.extensions_v2.handler_ops.session_queries import session_query_method
from jiuwenswarm.server.runtime.session.model_selection_store import (
    SESSION_MODEL_INFO_KEY,
    get_session_model_info,
    sanitize_model_info,
)
from jiuwenswarm.server.runtime.session.session_repo import read_session_repo
from jiuwenswarm.server.runtime.session.session_history_ops import (
    SessionHistoryError,
    prepare_extension_append,
    read_session_messages,
)
from jiuwenswarm.server.runtime.session.session_statistics import (
    SESSION_USAGE_INFO_KEY,
    read_session_turns,
    read_session_usage,
    select_usage_breakdown,
)


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


async def session_messages(
    ops: EventHandlerOps,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    """Read a fixed newest-first page from the bound main transcript.

    Raises:
        RuntimeError: No session is bound to this invocation.
        SessionHistoryError: Invalid parameters, cursor, snapshot or file I/O.
    """
    context = _require_session(ops, "session.messages")
    return await asyncio.to_thread(read_session_messages, context.session_id, limit, cursor)


async def session_append(
    ops: EventHandlerOps,
    message: object,
    *,
    bus: ExtensionEventBus | None = None,
) -> dict[str, object]:
    """Await session.append policy and commit a transcript-only message.

    Raises:
        RuntimeError: No session is bound to this invocation.
        SessionHistoryError: Invalid input, conflict, lifecycle, dispatch or I/O.
        asyncio.CancelledError: Cancellation, after draining an accepted write.
    """
    from jiuwenswarm.server.runtime.session.history_events import dispatch_session_append

    context = _require_session(ops, "session.append")
    if bus is None:
        raise SessionHistoryError("APPEND_BUS_UNAVAILABLE")
    package = ops.package_metadata
    package_id = package.package_id if package is not None else "<host>"
    prepared, message_id, key, fingerprint = prepare_extension_append(context.session_id, package_id, message)
    result = await dispatch_session_append(
        bus, context, prepared, message_id=message_id, source="extension", append_key=key,
        fingerprint=fingerprint, update_metadata=False,
    )
    return result.to_dict()
async def session_turns(ops: EventHandlerOps) -> int | None:
    """Read committed main user-input turns, or None for an unknown session."""
    context = _require_session(ops, "session.turns")
    return await asyncio.to_thread(read_session_turns, context.session_id)


async def session_usage(ops: EventHandlerOps, breakdown: bool = False) -> dict | None:
    """Read observed context and per-turn usage without measuring or billing."""
    context = _require_session(ops, "session.usage")
    if not is_valid_session_id(context.session_id):
        raise ValueError("invalid session_id")
    if SESSION_USAGE_INFO_KEY in context.metadata:
        snapshot = context.metadata[SESSION_USAGE_INFO_KEY]
        if snapshot is not None and (
            not isinstance(snapshot, Mapping) or snapshot.get("session_id") != context.session_id
        ):
            raise ValueError("session.usage requires a matching host snapshot")
        return select_usage_breakdown(snapshot, breakdown)
    return await asyncio.to_thread(read_session_usage, context.session_id, breakdown)


async def session_ops_build(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
    *,
    bus: ExtensionEventBus | None = None,
) -> EventHandlerOpsBuilder:
    """Contribute the session namespace to the host's shared Ops catalog."""
    del ops
    builder = await next_handler(event)
    if not isinstance(builder, EventHandlerOpsBuilder):
        raise TypeError("ops.build must return EventHandlerOpsBuilder")
    if bus is None:
        raise RuntimeError("session query Ops require the host EventBus")
    builder.add_namespace("session")
    for event_type in SESSION_QUERY_EVENTS:
        builder.add_method("session", event_type.split(".", 1)[1], session_query_method(bus, event_type))
    builder.add_method("session", "append", partial(session_append, bus=bus))
    return builder


class SessionFileService(Protocol):
    """Host-owned file data provider; no UI or package lifecycle ownership."""

    async def files(self, session_id: str, expanded_paths: list[str], channel_id: str) -> dict[str, Any]: ...

    async def read_file(
        self, session_id: str, path: str, channel_id: str, *, user_id: str = "",
    ) -> dict[str, Any]: ...


def register_session_ops(
    bus: ExtensionEventBus,
    *,
    file_service: SessionFileService | None = None,
    compact_service=None,
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

    async def read_file(ops: EventHandlerOps, *, path: str) -> dict[str, Any]:
        context = _require_session(ops, "session.read_file")
        if file_service is None:
            return {"status": "unavailable", "file": None}
        return await file_service.read_file(
            context.session_id,
            path,
            str(context.metadata.get("channel_id") or "web"),
            user_id=str(context.metadata.get("user_id") or ""),
        )

    async def build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        builder = await session_ops_build(event, ops, next_handler, bus=bus)
        builder.add_method("session", "files", files)
        builder.add_method("session", "read_file", read_file)
        async def compact(ops, *, instructions=None):
            if compact_service is None:
                from openjiuwen.core.context_engine import CompactionError
                raise CompactionError("COMPACT_UNSUPPORTED")
            return await compact_service.compact(ops, instructions=instructions)
        builder.add_method("session", "compact", compact)
        return builder

    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE,
        build,
        {},
        HandlerLevel.BUILTIN,
    )
