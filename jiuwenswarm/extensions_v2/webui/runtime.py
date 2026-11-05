# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI runtime: event registration, render entry and upstream dispatch."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
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
from jiuwenswarm.extensions_v2.webui.constants import SlotScope, WebUIEvent, slot_spec
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
        workspace_locator: Callable[[str, str], Awaitable[object]] | None = None,
    ) -> None:
        self.bus = bus
        self.transport = transport
        self.registry = registry or ActiveRenderRegistry()
        self._catalog = catalog or WebUICatalogService(lambda: ())
        self._locate_workspace = workspace_locator
        self._ready_clients: dict[tuple[str, str], None] = {}
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

        connection_id = request.target.connection_id or connection_id
        spec = slot_spec(request.target.slot_id)
        if spec is not None and spec.scope is SlotScope.APPLICATION and connection_id and not request.target.connection_id:
            request = replace(
                request,
                target=replace(request.target, scope=SlotScope.APPLICATION.value, connection_id=connection_id),
            )
        rejection = validate_upstream(request, self.registry)
        if rejection is not None:
            return rejection
        metadata = {"user_id": user_id, "channel_id": channel_id}
        if connection_id:
            metadata["ws_id"] = connection_id
        session_id = request.target.session_id.strip()
        cwd, project_root = await self._session_paths(session_id, channel_id) if session_id else (None, None)
        if project_root:
            metadata["project_root"] = project_root
        context = SessionContext(session_id=session_id, cwd=cwd, metadata=metadata) if session_id else None
        with self._connection_scope(connection_id, user_id=user_id, channel_id=channel_id):
            output = await self.bus.trigger(Event(request.event_type, request), context)
        return output if isinstance(output, UiActionOutput) else None

    async def _session_paths(self, session_id: str, channel_id: str) -> tuple[str | None, str | None]:
        """Return the host-declared cwd and project root of one session."""

        if self._locate_workspace is None:
            return None, None
        try:
            located = await self._locate_workspace(session_id, channel_id)
        except Exception:  # noqa: BLE001 - a lookup failure must not block dispatch
            logger.debug("[WebUI] session workspace lookup failed: %s", session_id, exc_info=True)
            return None, None
        if getattr(located, "status", "") != "ready":
            return None, None
        cwd = getattr(located, "cwd", None)
        project_root = getattr(located, "project_root", None)
        return (str(cwd) if cwd else None, str(project_root) if project_root else None)

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

    def remember_ready_client(self, connection_id: str, session_id: str) -> None:
        """Remember validated browser ownership even before a package contributes UI."""
        if connection_id:
            self._ready_clients[(connection_id, session_id)] = None
            while len(self._ready_clients) > 1024:
                self._ready_clients.pop(next(iter(self._ready_clients)))

    async def notify_packages_installed(self, package_ids: list[str]) -> bool:
        """Ask connected owners to replay ui.ready for newly installed packages."""
        delivered = False
        for connection_id, session_id in tuple(self._ready_clients):
            sent = await self.transport.notify_packages_installed(connection_id, session_id, package_ids)
            delivered = sent or delivered
        return delivered

    async def drop_connection(self, connection_id: str) -> None:
        """Release application views and prevent pending calls from recreating them."""
        for key in tuple(self._ready_clients):
            if key[0] == connection_id:
                self._ready_clients.pop(key, None)
        connection = self._connections.get(connection_id)
        if connection is not None:
            connection.closed = True
        self.registry.drop_connection(connection_id)

    async def drop_package(self, package_id: str) -> None:
        """Clear package views, retaining undelivered clears for cleanup retry."""

        for instance in self.registry.snapshot():
            if instance.target.package_id != package_id:
                continue
            async with self.registry.evict(instance) as current:
                if not current:
                    continue
                delivered = await self.transport.send_clear(clear_frame(instance, reason="package_unloaded"))
                if not delivered:
                    raise RuntimeError(f"Could not clear package view: {instance.target.key}")
                self.registry.drop_instance(instance.target)

    async def drop_session(self, session_id: str) -> None:
        """Remove all views of a session and notify the browser."""

        for key in tuple(self._ready_clients):
            if key[1] == session_id:
                self._ready_clients.pop(key, None)
        for instance in self.registry.drop_session(session_id):
            await self.transport.send_clear(clear_frame(instance, reason="session_closed"))

    async def shutdown(self) -> None:
        """Remove every active view."""

        self._ready_clients.clear()
        for connection in tuple(self._connections.values()):
            connection.closed = True
        for instance in self.registry.snapshot():
            await self.transport.send_clear(clear_frame(instance, reason="shutdown"))
            self.registry.drop_instance(instance.target)


def _as_session_context(session_context: SessionContext | str | None) -> SessionContext | None:
    if session_context is None or isinstance(session_context, SessionContext):
        return session_context
    return SessionContext(session_id=str(session_context))
