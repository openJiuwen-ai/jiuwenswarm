# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI runtime: event registration, render entry and upstream dispatch."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from weakref import WeakValueDictionary

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    ExtensionEventBus,
    HandlerLevel,
    SessionContext,
)

from jiuwenswarm.extensions_v2.event_types.webui import register_webui_events
from jiuwenswarm.extensions_v2.handler_ops.ui import build_webui_ops_build_handler
from jiuwenswarm.extensions_v2.webui.action_validation import validate_upstream
from jiuwenswarm.extensions_v2.webui.call_context import (
    WebUIConnectionContext,
    bind_connection,
    current_connection,
)
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
        self._connections: WeakValueDictionary[str, WebUIConnectionContext] = WeakValueDictionary()

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
            build_webui_ops_build_handler(target, WebUIEvent.RENDER.value, render=self.render),
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

        context = _as_session_context(session_context)
        connection = current_connection()
        metadata = context.metadata if context is not None else {}
        connection_id = (
            connection.connection_id if connection is not None
            else str(metadata.get("ws_id") or request.target.connection_id).strip()
        )
        with self._connection_scope(connection_id):
            output = await self.bus.trigger(Event(WebUIEvent.RENDER.value, request), context)
        return output if isinstance(output, UiRenderOutput) else None

    async def dispatch(
        self,
        request: UiActionInput,
        *,
        user_id: str = "",
        channel_id: str = "web",
        connection_id: str = "",
    ) -> UiActionOutput | None:
        """Upstream entry: turn a browser event into an event-chain call."""

        rejection = validate_upstream(request, self.registry)
        if rejection is not None:
            return rejection
        metadata = {"user_id": user_id, "channel_id": channel_id}
        connection_id = request.target.connection_id or connection_id
        if connection_id:
            metadata["ws_id"] = connection_id
        session_id = request.target.session_id.strip()
        context = SessionContext(session_id=session_id, metadata=metadata) if session_id else None
        with self._connection_scope(connection_id, user_id=user_id, channel_id=channel_id):
            output = await self.bus.trigger(Event(request.event_type, request), context)
        return output if isinstance(output, UiActionOutput) else None

    @contextmanager
    def _connection_scope(self, connection_id: str, *, user_id: str = "", channel_id: str = "web") -> Iterator[None]:
        connection = current_connection()
        if not connection_id:
            connection = None
        elif connection is None or connection.connection_id != connection_id:
            connection = self._connections.get(connection_id) or WebUIConnectionContext(
                connection_id, user_id=user_id, channel_id=channel_id,
            )
        if connection is not None:
            self._connections[connection_id] = connection
        with bind_connection(connection):
            yield

    async def drop_connection(self, connection_id: str) -> None:
        """Release application views and prevent pending calls from recreating them."""
        connection = self._connections.get(connection_id)
        if connection is not None:
            connection.closed = True
        self.registry.drop_connection(connection_id)

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

        for connection in tuple(self._connections.values()):
            connection.closed = True
        for instance in self.registry.snapshot():
            await self.transport.send_clear(clear_frame(instance, reason="shutdown"))
            self.registry.drop_instance(instance.target)


def _as_session_context(session_context: SessionContext | str | None) -> SessionContext | None:
    if session_context is None or isinstance(session_context, SessionContext):
        return session_context
    return SessionContext(session_id=str(session_context))
