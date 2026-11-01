# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway extension event inputs and outputs.

These objects are the contract between the Gateway trigger points, the event
bus and extension packages. Every event lives in this module so a package only
imports one place to build its input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

EVENT_GATEWAY_REQUEST = "gateway.request"

#: The single upstream WebUI method that carries browser ui.* events.
WEBUI_DISPATCH_METHOD = "webui.dispatch"

#: Synthetic session id used when a frame has no chat session. The event bus
#: requires a non-empty session id for every call.
GATEWAY_SESSION_PREFIX = "gateway:"


def _empty_mapping() -> Mapping[str, Any]:
    return MappingProxyType({})


def _empty_headers() -> Mapping[str, str]:
    return MappingProxyType({})


def gateway_session_id(connection_id: str) -> str:
    """Return the synthetic session id bound to one browser connection."""

    return f"{GATEWAY_SESSION_PREFIX}{connection_id}" if connection_id else GATEWAY_SESSION_PREFIX


class GatewayRoute(StrEnum):
    """Where a frame goes after ``gateway.request`` returns."""

    LOCAL = "local"
    FORWARD = "forward"
    CONSUME = "consume"
    DENY = "deny"

    @classmethod
    def parse(cls, value: object) -> "GatewayRoute | None":
        """Return the matching route, or None for an unknown value."""

        if isinstance(value, GatewayRoute):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                return None
        return None


@dataclass(frozen=True, slots=True)
class GatewayRequestInput:
    """One inbound WS frame before native dispatch."""

    method: str
    params: Mapping[str, Any] = field(default_factory=_empty_mapping)
    session_id: str | None = None
    connection_id: str = ""
    user_id: str | None = None
    channel_id: str = "web"


@dataclass(frozen=True, slots=True)
class GatewayRequestOutput:
    """Frame disposition decided by the handler chain."""

    handled: bool = False
    route: str = GatewayRoute.FORWARD.value
    params: Mapping[str, Any] | None = None
    response: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    meta: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class HttpFetchInput:
    """One outbound HTTP request made by a Gateway extension."""

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=_empty_headers)
    body: Any = None
    timeout_ms: int = 3000
    follow_redirects: bool = False


@dataclass(frozen=True, slots=True)
class HttpFetchOutput:
    """Result of one ``ops.http.fetch`` call."""

    status: int = 0
    headers: Mapping[str, str] = field(default_factory=_empty_headers)
    body: Any = None
    elapsed_ms: int = 0
    code: str | None = None
