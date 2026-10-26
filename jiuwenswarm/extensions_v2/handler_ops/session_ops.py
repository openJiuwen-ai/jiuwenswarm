# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only session workspace capabilities contributed during ``ops.build``."""

from __future__ import annotations

from pathlib import Path

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
    return builder


def register_session_ops(bus: ExtensionEventBus) -> None:
    """Register workspace capabilities before Registry initializes Ops once."""
    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE,
        session_ops_build,
        {},
        HandlerLevel.BUILTIN,
    )
