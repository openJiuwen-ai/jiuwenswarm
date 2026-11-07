# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the connection facts the Gateway bound to one event call.

The Gateway has no AgentServer context variable; every CORE handler reads the
facts from the ``SessionContext`` metadata set by the trigger point.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from openjiuwen.core.events import EventHandlerOps

from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Caller:
    """The package activation a host operation acts for, resolved from the operation's Ops.

    Host operations (``ops.ui.render`` and the host actions) carry it through their event chain
    out of band, so USER handlers of other packages cannot change who the call is for.
    ``current(target)`` re-checks after any await that the activation is still the published one;
    with a target it also checks that the target belongs to it and the connection is still open.
    """

    package_id: str
    generation: str
    connection_id: str
    current: Callable[[RenderTarget | None], bool]


CallerFactory = Callable[[EventHandlerOps], "Caller | None"]
_CALLER: ContextVar[Caller | None] = ContextVar("gateway_extension_caller", default=None)


@contextmanager
def caller_scope(caller: Caller | None) -> Iterator[None]:
    """Carry the host operation's caller through its event chain."""
    token = _CALLER.set(caller)
    try:
        yield
    finally:
        _CALLER.reset(token)


def current_caller() -> Caller | None:
    """The caller of the host operation in progress, or None outside one."""
    return _CALLER.get()


def render_is_current(target: RenderTarget) -> bool:
    """Recheck the caller at a commit boundary; a host without callers has nothing to check."""
    caller = _CALLER.get()
    return caller is None or caller.current(target)


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
