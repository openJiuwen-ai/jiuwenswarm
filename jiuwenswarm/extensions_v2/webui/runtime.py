# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI runtime: event registration, render entry and upstream dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from weakref import WeakValueDictionary

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    EventHandlerOps,
    ExtensionEventBus,
    HandlerLevel,
    SessionContext,
)
from openjiuwen.core.extension.data import JsonState, StateAddress, StateDependencies

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
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.schema import apply_slot_scope
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StateRenderSource:
    """Trusted route and last delivered request used for an automatic rebuild."""

    request: UiRenderInput
    session_context: SessionContext | None
    connection_id: str
    user_id: str
    channel_id: str


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
        self.resources = WebUIResourceService(self._catalog)
        self._locate_workspace = workspace_locator
        self._ready_clients: dict[tuple[str, str], None] = {}
        self._connections: WeakValueDictionary[str, WebUIConnectionContext] = WeakValueDictionary()
        self._states: JsonState | None = None
        self._state_dependencies: StateDependencies | None = None
        self._state_unsubscribe: Callable[[], None] | None = None
        self._state_loop: asyncio.AbstractEventLoop | None = None
        self._state_sources: dict[str, _StateRenderSource] = {}
        self._state_refresh_pending: set[str] = set()
        self._state_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._state_stopped = False

    def bind_state(self, states: JsonState, dependencies: StateDependencies) -> None:
        """Bind Core's state collector to delivered WebUI targets once."""

        if self._state_unsubscribe is not None:
            raise RuntimeError("WebUI state dependencies are already bound")
        self._states = states
        self._state_dependencies = dependencies
        self._state_loop = asyncio.get_running_loop()
        self._state_stopped = False
        self._state_unsubscribe = states.changes.subscribe(self._on_state_changed)

    def register_event_types(self, bus: ExtensionEventBus | None = None) -> None:
        """Register ui.* event types and their CORE handlers."""

        target = bus or self.bus
        register_webui_events(
            target,
            build_render_core_handler(
                self.transport,
                self.registry,
                on_delivered=self._state_render_delivered,
                on_dropped=self._drop_state_target,
            ),
            build_action_core_handler(self.registry),
        )

    def register_handlers(self, bus: ExtensionEventBus | None = None) -> None:
        """Register the BUILTIN handler that publishes ``ops.ui.render``."""

        target = bus or self.bus
        target.register_event_handler(
            OPS_BUILD_EVENT_TYPE,
            build_webui_ops_build_handler(
                target, WebUIEvent.RENDER.value, render=self.render, resources=self.resources,
            ),
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
        user_id = connection.user_id if connection is not None else str(metadata.get("user_id") or "")
        channel_id = connection.channel_id if connection is not None else str(metadata.get("channel_id") or "web")
        output = await self._render_with_route(
            request,
            context,
            connection_id=connection_id,
            user_id=user_id,
            channel_id=channel_id,
        )
        return output if isinstance(output, UiRenderOutput) else None

    async def _render_with_route(
        self,
        request: UiRenderInput,
        context: SessionContext | None,
        *,
        connection_id: str,
        user_id: str,
        channel_id: str,
    ) -> object:
        dependencies = self._state_dependencies
        with self._connection_scope(connection_id, user_id=user_id, channel_id=channel_id):
            if dependencies is None:
                return await self.bus.trigger(Event(WebUIEvent.RENDER.value, request), context)
            target, _ = apply_slot_scope(request.target, connection_id)
            with dependencies.collect(target.key):
                return await self.bus.trigger(Event(WebUIEvent.RENDER.value, request), context)

    async def _state_render_delivered(
        self,
        request: UiRenderInput,
        ops: EventHandlerOps | None,
    ) -> None:
        """Commit reads only after the exact frame reached the transport."""

        states = self._states
        dependencies = self._state_dependencies
        # Core stores the active build in a ContextVar. It remains bound to
        # this render task across transport awaits and cannot observe another
        # target's concurrent collector.
        render_id = dependencies.current_build if dependencies is not None else None
        if states is None or dependencies is None or render_id is None:
            return
        reads = dict(dependencies.reads(render_id))
        dependencies.commit(render_id, request.target.key)
        instance = self.registry.get(request.target)
        if instance is None:
            dependencies.drop_target(request.target.key)
            self._state_sources.pop(request.target.key, None)
            return
        if reads:
            connection = current_connection()
            context = ops.session_context if ops is not None else None
            delivered = UiRenderInput(
                target=instance.target,
                root=instance.root,
                mode=instance.mode,
                visible_when=instance.visible_when,
            )
            self._state_sources[request.target.key] = _StateRenderSource(
                request=delivered,
                session_context=context,
                connection_id=instance.target.connection_id,
                user_id=connection.user_id if connection is not None else "",
                channel_id=connection.channel_id if connection is not None else "web",
            )
        else:
            self._state_sources.pop(request.target.key, None)
        if reads and await states.changed_since(reads):
            self._schedule_state_targets((request.target.key,))

    def _on_state_changed(self, address: StateAddress, version: int) -> None:
        """Enqueue affected targets without running UI work in the state write path."""

        del version
        dependencies = self._state_dependencies
        loop = self._state_loop
        if self._state_stopped or dependencies is None or loop is None:
            return
        targets = dependencies.targets(address)
        if targets:
            loop.call_soon_threadsafe(self._schedule_state_targets, targets)

    def _schedule_state_targets(self, target_keys: tuple[str, ...]) -> None:
        if self._state_stopped:
            return
        for target_key in target_keys:
            if target_key not in self._state_sources:
                continue
            self._state_refresh_pending.add(target_key)
            task = self._state_refresh_tasks.get(target_key)
            if task is not None and not task.done():
                continue
            task = asyncio.create_task(
                self._refresh_state_target(target_key),
                name=f"webui-state-refresh:{target_key}",
            )
            self._state_refresh_tasks[target_key] = task
            task.add_done_callback(
                lambda completed, key=target_key: self._state_refresh_done(key, completed)
            )

    async def _refresh_state_target(self, target_key: str) -> None:
        while target_key in self._state_refresh_pending and not self._state_stopped:
            self._state_refresh_pending.discard(target_key)
            source = self._state_sources.get(target_key)
            if source is None:
                return
            instance = self.registry.get(source.request.target)
            if instance is None:
                self._drop_state_target(source.request.target)
                return
            request = UiRenderInput(
                target=instance.target,
                root=instance.root,
                mode=instance.mode,
                visible_when=instance.visible_when,
            )
            await self._render_with_route(
                request,
                source.session_context,
                connection_id=source.connection_id,
                user_id=source.user_id,
                channel_id=source.channel_id,
            )

    def _state_refresh_done(self, target_key: str, task: asyncio.Task[None]) -> None:
        if self._state_refresh_tasks.get(target_key) is task:
            self._state_refresh_tasks.pop(target_key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[WebUI] state-driven refresh failed: %s",
                target_key,
                exc_info=(type(error), error, error.__traceback__),
            )
        if target_key in self._state_refresh_pending and not self._state_stopped:
            self._schedule_state_targets((target_key,))

    def _drop_state_target(self, target: RenderTarget) -> None:
        dependencies = self._state_dependencies
        if dependencies is not None:
            dependencies.drop_target(target.key)
        self._state_sources.pop(target.key, None)
        self._state_refresh_pending.discard(target.key)
        task = self._state_refresh_tasks.get(target.key)
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()

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
        if spec is not None and request.target.resolved_scope == SlotScope.APPLICATION.value:
            if connection_id and not request.target.connection_id:
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

    async def notify_packages_changed(self, package_ids: list[str]) -> bool:
        """Ask connected owners to replay ui.ready after a package changes."""
        delivered = False
        for connection_id, session_id in tuple(self._ready_clients):
            sent = await self.transport.notify_packages_changed(connection_id, session_id, package_ids)
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
        await self.resources.drop(connection_id=connection_id)
        for instance in self.registry.drop_connection(connection_id):
            self._drop_state_target(instance.target)

    async def drop_package(self, package_id: str, *, generation: int | None = None) -> None:
        """Clear package views, retaining undelivered clears for cleanup retry."""

        for instance in self.registry.snapshot():
            if instance.target.package_id != package_id:
                continue
            async with self.registry.evict(instance) as current:
                if not current:
                    continue
                frame = clear_frame(instance, reason="package_unloaded")
                delivered = await self.transport.send_clear(frame)
                if not delivered:
                    raise RuntimeError(f"Could not clear package view: {instance.target.key}")
                self.registry.drop_instance(instance.target, retired_revision=frame.revision)
                self._drop_state_target(instance.target)
        await self.resources.drop(package_id=package_id, generation=generation)

    async def drop_session(self, session_id: str) -> None:
        """Remove all views of a session and notify the browser."""

        for key in tuple(self._ready_clients):
            if key[1] == session_id:
                self._ready_clients.pop(key, None)
        await self.resources.drop(session_id=session_id)
        for instance in self.registry.drop_session(session_id):
            self._drop_state_target(instance.target)
            await self.transport.send_clear(clear_frame(instance, reason="session_closed"))

    async def stop_state_refresh(self) -> None:
        """Stop state-driven renders before extension handlers are removed."""

        self._state_stopped = True
        if self._state_unsubscribe is not None:
            self._state_unsubscribe()
            self._state_unsubscribe = None
        tasks = tuple(self._state_refresh_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._state_refresh_tasks.clear()
        self._state_refresh_pending.clear()

    async def shutdown(self) -> None:
        """Remove every active view."""

        await self.stop_state_refresh()
        self._ready_clients.clear()
        await self.resources.shutdown()
        for connection in tuple(self._connections.values()):
            connection.closed = True
        for instance in self.registry.snapshot():
            await self.transport.send_clear(clear_frame(instance, reason="shutdown"))
            self.registry.drop_instance(instance.target)
            self._drop_state_target(instance.target)
        self.registry.clear()
        self._state_sources.clear()


def _as_session_context(session_context: SessionContext | str | None) -> SessionContext | None:
    if session_context is None or isinstance(session_context, SessionContext):
        return session_context
    return SessionContext(session_id=str(session_context))
