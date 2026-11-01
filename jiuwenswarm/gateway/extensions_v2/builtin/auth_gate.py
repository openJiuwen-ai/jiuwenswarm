# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authentication gate: the BUILTIN handler that keeps locked frames out.

The gate owns one platform rule: a browser connection that has not logged in
must not reach the native dispatch path. Authentication itself is never decided
here. A USER handler performs the login (usually through ``ops.http.fetch``)
and records the result as a connection-scoped ``ui.store`` value under
:data:`AUTH_STORE_KEY`; the gate only reads that fact.

The level matters. BUILTIN handlers run after every USER handler and before
the CORE handler, so a package that answers a frame without forwarding bypasses
the gate. This is the same ordering contract as AgentServer: packages observe
and replace platform behaviour, the platform rule only applies to frames that
actually continue inward.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    WEBUI_DISPATCH_METHOD,
    GatewayRequestInput,
    GatewayRequestOutput,
    GatewayRoute,
)
from jiuwenswarm.gateway.extensions_v2.state import SCOPE_CONNECTION, UiStateStore

logger = logging.getLogger(__name__)

#: Connection-scoped key holding the authenticated session of one browser tab.
AUTH_STORE_KEY = "auth.session"

#: Methods a locked connection may still call: the browser has to be able to
#: mount the login gate and report a click on it. ``webui.dispatch`` reaches
#: the gate only when no USER click handler answered the frame earlier.
LOCKED_ALLOWED_METHODS = frozenset({WEBUI_DISPATCH_METHOD, "ping"})

CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_REQUIRED_MESSAGE = "login required"

LockedCallback = Callable[[str], Awaitable[None]]


def is_authenticated(store: UiStateStore, connection_id: str) -> bool:
    """Return True when the connection holds a live authentication record."""

    if not connection_id:
        return False
    return store.get(scope=SCOPE_CONNECTION, scope_id=connection_id, key=AUTH_STORE_KEY) is not None


def build_auth_gate_handler(
    store: UiStateStore,
    *,
    on_locked: LockedCallback | None = None,
    enforce: Callable[[], bool] | None = None,
) -> EventHandler:
    """Build the BUILTIN handler registered for ``gateway.request``.

    ``on_locked`` is called with the connection id before a frame is denied, so
    the host can put the login gate on screen without the gate owning render
    logic.

    ``enforce`` reports whether the gate applies at all. The host passes
    ``runtime.has_auth_provider`` so an installation without a login-owning
    package never shows a login form; ``None`` keeps the gate always on for
    standalone use and tests.
    """

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
        if _lets_through(request, store):
            return await next_handler(event)

        if on_locked is not None and request.connection_id:
            try:
                await on_locked(request.connection_id)
            except Exception as exc:  # noqa: BLE001 - a hint failure must not open the gate
                logger.warning("[GatewayExtensions] login gate render failed: %s", exc)
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


def _lets_through(request: GatewayRequestInput, store: UiStateStore) -> bool:
    """Return True when the frame may continue towards the CORE handler."""

    # A host-triggered event has no browser connection to authenticate.
    if not request.connection_id:
        return True
    # Let the CORE handler answer a malformed frame with BAD_REQUEST.
    if not isinstance(request.method, str) or not request.method.strip():
        return True
    if request.method in LOCKED_ALLOWED_METHODS:
        return True
    return is_authenticated(store, request.connection_id)
