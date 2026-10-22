# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI runtime: event registration, render entry and upstream dispatch."""

from __future__ import annotations

import logging

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    ExtensionEventBus,
    HandlerLevel,
    SessionContext,
)

from jiuwenswarm.extensions_v2.event_types.webui import register_webui_events
from jiuwenswarm.extensions_v2.handler_ops.ui import build_webui_ops_build_handler
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.core_handlers.action_core import build_action_core_handler
from jiuwenswarm.extensions_v2.webui.core_handlers.render_core import build_render_core_handler, clear_frame
from jiuwenswarm.extensions_v2.webui.models import (
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

logger = logging.getLogger(__name__)


class WebUIRuntime:
    """Own the WebUI event types, render registry and downstream transport."""

    def __init__(
        self,
        *,
        bus: ExtensionEventBus,
        transport: RenderTransport,
        registry: ActiveRenderRegistry | None = None,
        catalog: WebUICatalogService | None = None,
    ) -> None:
        self.bus = bus
        self.transport = transport
        self.registry = registry or ActiveRenderRegistry()
        self._catalog = catalog or WebUICatalogService(lambda: ())

    def register_event_types(self, bus: ExtensionEventBus | None = None) -> None:
        """Register ui.* event types and their CORE handlers."""

        target = bus or self.bus
        register_webui_events(
            target,
            build_render_core_handler(self.transport, self.registry),
            build_action_core_handler(self.registry),
        )

    def register_handlers(self, bus: ExtensionEventBus | None = None) -> None:
        """Register the BUILTIN handler that publishes ``ops.ui.render``."""

        target = bus or self.bus
        target.register_event_handler(
            OPS_BUILD_EVENT_TYPE,
            build_webui_ops_build_handler(target, WebUIEvent.RENDER.value),
            {},
            HandlerLevel.BUILTIN,
        )

    def register_events(self, bus: ExtensionEventBus | None = None) -> None:
        """Register event types and handlers for standalone hosts and tests."""

        self.register_event_types(bus)
        self.register_handlers(bus)

    def catalog(self) -> WebUICatalogService:
        """Return the catalog derived from the active packages."""

        return self._catalog

    async def render(
        self,
        request: UiRenderInput,
        session_context: SessionContext | str | None = None,
    ) -> UiRenderOutput | None:
        """Host entry: trigger ui.render; framing and delivery happen in the CORE handler."""

        output = await self.bus.trigger(
            Event(WebUIEvent.RENDER.value, request),
            _as_session_context(session_context),
        )
        return output if isinstance(output, UiRenderOutput) else None

    async def dispatch(
        self,
        request: UiActionInput,
        *,
        user_id: str = "",
        channel_id: str = "web",
    ) -> UiActionOutput | None:
        """Upstream entry: turn a browser event into an event-chain call."""

        context = SessionContext(
            session_id=request.target.session_id,
            metadata={"user_id": user_id, "channel_id": channel_id},
        )
        output = await self.bus.trigger(Event(request.event_type, request), context)
        return output if isinstance(output, UiActionOutput) else None

    async def drop_package(self, package_id: str) -> None:
        """Remove all views of a package and notify the browser."""

        for instance in self.registry.drop_package(package_id):
            await self.transport.send_clear(clear_frame(instance, reason="package_unloaded"))

    async def drop_session(self, session_id: str) -> None:
        """Remove all views of a session and notify the browser."""

        for instance in self.registry.drop_session(session_id):
            await self.transport.send_clear(clear_frame(instance, reason="session_closed"))

    async def shutdown(self) -> None:
        """Remove every active view."""

        for instance in self.registry.snapshot():
            await self.transport.send_clear(clear_frame(instance, reason="shutdown"))
            self.registry.drop_instance(instance.target)


def _as_session_context(session_context: SessionContext | str | None) -> SessionContext | None:
    if session_context is None or isinstance(session_context, SessionContext):
        return session_context
    return SessionContext(session_id=str(session_context))
