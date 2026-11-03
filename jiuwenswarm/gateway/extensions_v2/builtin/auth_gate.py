# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authentication gate: the BUILTIN handler that keeps locked frames out.

The gate owns one platform rule: a browser connection without an authorization
header must not reach the native dispatch path. Authentication itself is never
decided here, and the platform never renders a login view. The browser stores
whatever credential a package asks for, sends it in
``request_context.headers.authorization`` on every frame, and a USER handler
decides whether that credential is valid. The gate only checks that the header
is present, so it never parses, caches or revokes credentials.

The level matters. BUILTIN handlers run after every USER handler and before
the CORE handler, so a package that answers a frame without forwarding bypasses
the gate. This is the same ordering contract as AgentServer: packages observe
and replace platform behaviour, the platform rule only applies to frames that
actually continue inward.

Two generic exemptions keep a plugin-rendered interaction reachable while the
connection is locked: ``ping`` stays open for keepalive, and frames targeting an
application-scoped slot the Gateway owns are answered locally. Those slots have
no AgentServer view, so a package paints and serves them through the Gateway
``ui.render`` / ``ui.click`` chain that runs before this gate.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Callable

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.gateway.extensions_v2.config import AuthPolicy
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    WEBUI_DISPATCH_METHOD,
    GatewayRequestInput,
    GatewayRequestOutput,
    GatewayRoute,
)
from jiuwenswarm.gateway.extensions_v2.webui.constants import SlotScope, WebUIEvent, slot_spec

logger = logging.getLogger(__name__)

CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_REQUIRED_MESSAGE = "login required"

#: Framing/keepalive methods that must work before a login exists.
ANONYMOUS_METHODS = frozenset({"ping"})


def has_authorization(headers: Mapping[str, str] | None) -> bool:
    """Return True when the frame carries a non-empty authorization header."""

    if not isinstance(headers, Mapping):
        return False
    for name, value in headers.items():
        if isinstance(name, str) and name.strip().lower() == "authorization":
            return isinstance(value, str) and bool(value.strip())
    return False


def build_auth_gate_handler(
    *,
    policy: AuthPolicy | None = None,
    enforce: Callable[[], bool] | None = None,
) -> EventHandler:
    """Build the BUILTIN handler registered for ``gateway.request``.

    ``enforce`` reports whether the gate applies at all. The host passes
    ``runtime.has_auth_provider`` so an installation without a login-owning
    package never gates frames; ``None`` keeps the gate always on for
    standalone use and tests.
    """

    active_policy = policy or AuthPolicy()

    async def auth_gate(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> GatewayRequestOutput:
        del ops
        request = event.event_input
        if not isinstance(request, GatewayRequestInput):
            raise TypeError(f"{EVENT_GATEWAY_REQUEST} requires GatewayRequestInput")
        if enforce is not None and not enforce():
            return await next_handler(event)
        if not _gated(request, active_policy):
            return await next_handler(event)

        gateway_slot_event = _gateway_slot_event(request)
        if gateway_slot_event == WebUIEvent.READY.value:
            # The browser mounted a Gateway-owned slot; nothing to forward.
            return _consumed({"event": WebUIEvent.READY.value, "accepted": True})
        if gateway_slot_event == WebUIEvent.CHANGE.value:
            # Field values are local to the browser form, so the draft is dropped.
            return _consumed({})

        if has_authorization(request.headers):
            return await next_handler(event)
        if request.method in ANONYMOUS_METHODS:
            return await next_handler(event)
        if gateway_slot_event:
            # No package handled this interaction on a Gateway-owned slot; the
            # slot has no AgentServer view, so answer it here instead.
            return _consumed({})

        logger.info(
            "[GatewayExtensions] frame denied before login: method=%s ws_id=%s",
            request.method,
            request.connection_id,
        )
        return GatewayRequestOutput(
            handled=True,
            route=GatewayRoute.DENY.value,
            error={"code": CODE_AUTH_REQUIRED, "message": AUTH_REQUIRED_MESSAGE},
        )

    return auth_gate


def _gated(request: GatewayRequestInput, policy: AuthPolicy) -> bool:
    """Return True when the platform rule applies to this frame at all."""

    if not policy.enabled:
        return False
    # A host-triggered event has no browser connection to authenticate.
    if not request.connection_id:
        return False
    # Let the CORE handler answer a malformed frame with BAD_REQUEST.
    return isinstance(request.method, str) and bool(request.method.strip())


def _gateway_slot_event(request: GatewayRequestInput) -> str:
    """Return the upstream event name when the frame targets a Gateway-owned slot."""

    if request.method != WEBUI_DISPATCH_METHOD or not isinstance(request.params, Mapping):
        return ""
    target = request.params.get("target")
    slot_id = target.get("slot_id") if isinstance(target, Mapping) else ""
    if not isinstance(slot_id, str):
        return ""
    spec = slot_spec(slot_id)
    if spec is None or spec.scope is not SlotScope.APPLICATION:
        return ""
    event_type = request.params.get("event")
    return event_type.strip().lower() if isinstance(event_type, str) else ""


def _consumed(response: Mapping[str, object]) -> GatewayRequestOutput:
    return GatewayRequestOutput(
        handled=True,
        route=GatewayRoute.CONSUME.value,
        response=response,
    )
