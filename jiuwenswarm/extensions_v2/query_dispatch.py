# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Platform query dispatch, trusted caller attribution and scoped reentry checks."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
import copy
from dataclasses import dataclass
from types import MappingProxyType

from openjiuwen.core.events import Event, EventHandlerOps, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.session_queries import (
    SessionQueryDenied, SessionQueryError, valid_query_input, valid_query_output,
)
from jiuwenswarm.server.runtime.session.session_history_ops import require_session_id


@dataclass(frozen=True, slots=True)
class SessionQueryOrigin:
    """Original calling package, independent of the entered query Hook's owner."""

    package: str | None


@dataclass(slots=True)
class _QueryFrame:
    key: tuple[int, str, str]
    origin: SessionQueryOrigin
    active: bool = True


_FRAMES: ContextVar[tuple[_QueryFrame, ...]] = ContextVar("session_query_frames", default=())
MAX_QUERY_DEPTH = 16


def current_query_origin() -> SessionQueryOrigin | None:
    """Return the host-stamped caller during a query; None outside Ops dispatch.

    This server helper does not claim Core's EventNext has Claude's origin API.
    No caller tier is invented when the current Core does not expose one.
    """
    frames = [frame for frame in _FRAMES.get() if frame.active]
    return frames[-1].origin if frames else None


def require_query_context(ops: EventHandlerOps | None) -> SessionContext:
    """Require an already-bound session without consulting user Event arguments."""
    context = None if ops is None else ops.session_context
    if context is None:
        raise RuntimeError("session query requires SessionContext")
    require_session_id(context.session_id)
    return context


def detached_copy(value: object) -> object:
    """Return independent plain data even from Core-frozen nested mappings."""
    if isinstance(value, Mapping):
        return {key: detached_copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [detached_copy(item) for item in value]
    return copy.deepcopy(value)


async def dispatch_session_query(
    bus: ExtensionEventBus,
    ops: EventHandlerOps,
    event_type: str,
    arguments: Mapping[str, object],
) -> object:
    """Raise one query event, preserve the trusted binding, validate and unwrap."""
    context = require_query_context(ops)
    if not valid_query_input(event_type, arguments):
        raise TypeError(f"invalid arguments for {event_type}")
    frames = tuple(frame for frame in _FRAMES.get() if frame.active)
    key = (id(bus), context.session_id, event_type)
    if any(frame.key == key for frame in frames):
        raise SessionQueryError("QUERY_REENTRANT")
    if len(frames) >= MAX_QUERY_DEPTH:
        raise SessionQueryError("QUERY_DEPTH_EXCEEDED")
    package = ops.package_metadata
    frame = _QueryFrame(key, SessionQueryOrigin(None if package is None else package.package_id))
    token = _FRAMES.set((*frames, frame))
    try:
        result = await bus.trigger(Event(event_type, MappingProxyType(dict(arguments))), context)
        if not valid_query_output(event_type, result):
            raise SessionQueryError("INVALID_QUERY_RESULT")
        if "deny" in result:
            raise SessionQueryDenied(result["deny"])
        return detached_copy(result["value"])
    finally:
        # Child tasks inherit a tuple pointing at this frame. Marking it inactive
        # prevents an inherited, completed call from becoming a false recursion.
        frame.active = False
        _FRAMES.reset(token)
