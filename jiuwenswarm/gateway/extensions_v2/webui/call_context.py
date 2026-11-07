# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the connection facts the Gateway bound to one event call.

The Gateway has no AgentServer context variable; every CORE handler reads the
facts from the ``SessionContext`` metadata set by the trigger point.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from openjiuwen.core.events import EventHandlerOps

from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget

_EMPTY: Mapping[str, Any] = MappingProxyType({})
RenderCheck = Callable[[RenderTarget], bool]
RenderCheckFactory = Callable[[EventHandlerOps], RenderCheck]
_RENDER_CHECK: ContextVar[RenderCheck | None] = ContextVar("gateway_render_check", default=None)


@contextmanager
def render_scope(check: RenderCheck | None) -> Iterator[None]:
    """Carry the host's check of the original Ops through the render event chain."""
    token = _RENDER_CHECK.set(check)
    try:
        yield
    finally:
        _RENDER_CHECK.reset(token)


def render_is_current(target: RenderTarget) -> bool:
    """Recheck the captured caller at the commit boundary; standalone hosts have no lease."""
    check = _RENDER_CHECK.get()
    return check is None or check(target)


def session_metadata(ops: EventHandlerOps | None) -> Mapping[str, Any]:
    """Return the frozen metadata mapping of this call, or an empty mapping."""

    context = getattr(ops, "session_context", None) if ops is not None else None
    metadata = getattr(context, "metadata", None) if context is not None else None
    if not isinstance(metadata, Mapping):
        return _EMPTY
    return metadata


def trusted_connection(ops: EventHandlerOps | None) -> str:
    """Read the connection id the Gateway bound to this event call."""

    metadata = session_metadata(ops)
    return str(metadata.get("connection_id") or metadata.get("ws_id") or "").strip()


def trusted_session(ops: EventHandlerOps | None) -> str:
    """Read the chat session id carried by the inbound frame, if any."""

    return str(session_metadata(ops).get("session_id") or "").strip()
