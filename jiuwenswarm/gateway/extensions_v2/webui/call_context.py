# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the connection facts the Gateway bound to one event call.

The Gateway has no AgentServer context variable; every CORE handler reads the
facts from the ``SessionContext`` metadata set by the trigger point.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from openjiuwen.core.events import EventHandlerOps

from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Caller:
    """The package activation a host operation acts for, resolved from the operation's Ops.

    Host operations (``ops.ui.render`` and the host actions) pass it to ``bus.trigger`` as the
    chain's ``trigger_context``, which only the CORE handler receives: USER handlers cannot read or
    change who the call is for, and nested triggers or tasks they start do not inherit it.
    ``current(target)`` re-checks after any await that the activation is still the published one;
    with a target it also checks that the target belongs to it and the connection is still open.
    """

    package_id: str
    generation: str
    connection_id: str
    current: Callable[[RenderTarget | None], bool]


CallerFactory = Callable[[EventHandlerOps], "Caller | None"]


def caller_of(ops: EventHandlerOps | None) -> Caller | None:
    """The caller a CORE handler received as ``trigger_context``; None for a chain the host started."""

    context = getattr(ops, "trigger_context", None) if ops is not None else None
    return context if isinstance(context, Caller) else None


def caller_generation(caller: Caller | None, target: RenderTarget) -> str | None:
    """The generation a frame for ``target`` carries when the caller sent it; None for the host's own frames."""

    return caller.generation if caller is not None and caller.package_id == target.package_id else None


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
