# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the connection facts the Gateway bound to one event call.

The Gateway has no AgentServer context variable; every CORE handler reads the
facts from the ``SessionContext`` metadata set by the trigger point.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from openjiuwen.core.events import EventHandlerOps

_EMPTY: Mapping[str, Any] = MappingProxyType({})


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
