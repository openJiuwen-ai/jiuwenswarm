# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway WebUI runtime: event registration, render entry and scoped cleanup.

Mirrors ``jiuwenswarm/extensions_v2/webui/runtime.py``. The Gateway owns the
browser socket, so the downstream transport addresses frames by web socket id.
The platform renders nothing by itself: every tree comes from a package through
``ops.ui.render``.
"""

from __future__ import annotations

import logging

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    ExtensionEventBus,
    HandlerLevel,
    SessionContext,
)

from jiuwenswarm.gateway.extensions_v2.event_types.webui import register_webui_events
from jiuwenswarm.gateway.extensions_v2.handler_ops.ui import build_ui_ops_build_handler
from jiuwenswarm.gateway.extensions_v2.models import EVENT_UI_RENDER, gateway_session_id
from jiuwenswarm.gateway.extensions_v2.webui.constants import DEFAULT_CHANNEL_ID
from jiuwenswarm.gateway.extensions_v2.webui.call_context import RenderCheckFactory
from jiuwenswarm.gateway.extensions_v2.webui.core_handlers.render_core import (
    build_render_core_handler,
    clear_frame,
)
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.gateway.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.gateway.extensions_v2.webui.transport import GatewayTransport

logger = logging.getLogger(__name__)


class GatewayWebUIRuntime:
    """Own the WebUI event types, the render registry and the transport."""

    def __init__(
        self,
        *,
        bus: ExtensionEventBus,
        transport: GatewayTransport,
        registry: ActiveRenderRegistry | None = None,
        render_check: RenderCheckFactory | None = None,
    ) -> None:
        self.bus = bus
        self.transport = transport
        self.registry = registry or ActiveRenderRegistry()
        self._render_check = render_check

    # ── registration ──────────────────────────────────────────────────────

    def register_event_types(self, bus: ExtensionEventBus | None = None) -> None:
        """Register the ``ui.render`` event type and its CORE handler."""

        target = bus or self.bus
        register_webui_events(
            target,
            build_render_core_handler(self.transport, self.registry),
        )

    def register_handlers(self, bus: ExtensionEventBus | None = None) -> list[str]:
        """Register the BUILTIN handler that publishes ``ops.ui``; return its id."""

        target = bus or self.bus
        return [
            target.register_event_handler(
                OPS_BUILD_EVENT_TYPE,
                build_ui_ops_build_handler(target, render_check=self._render_check),
                {},
                HandlerLevel.BUILTIN,
            )
        ]

    def register_events(self, bus: ExtensionEventBus | None = None) -> list[str]:
        """Register event types and handlers for standalone hosts and tests."""

        self.register_event_types(bus)
        return self.register_handlers(bus)

    # ── render entry ──────────────────────────────────────────────────────

    async def render(
        self,
        request: UiRenderInput,
        session_context: SessionContext | None = None,
    ) -> UiRenderOutput | None:
        """Trigger ``ui.render``; framing and delivery happen in the CORE handler."""

        context = session_context if isinstance(session_context, SessionContext) else None
        if context is None:
            connection_id = request.target.connection_id
            context = SessionContext(
                gateway_session_id(connection_id),
                None,
                {"connection_id": connection_id, "channel_id": DEFAULT_CHANNEL_ID},
            )
        output = await self.bus.trigger(Event(EVENT_UI_RENDER, request), context)
        return output if isinstance(output, UiRenderOutput) else None

    # ── scoped cleanup ────────────────────────────────────────────────────

    async def drop_connection(self, connection_id: str) -> None:
        """Release every application view owned by one socket."""

        if not connection_id:
            return
        self.registry.drop_connection(connection_id)

    async def shutdown(self) -> None:
        """Remove every active view."""

        for instance in self.registry.snapshot():
            await self.transport.send_clear(clear_frame(instance, reason="shutdown"))
            self.registry.drop_instance(instance.target)
        self.registry.clear()
