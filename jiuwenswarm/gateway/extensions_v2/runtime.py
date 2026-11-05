# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side extension runtime.

One runtime owns one event bus for the Gateway process. It registers the
Gateway event types, runs the single allowed ``ops.build``, publishes USER
handlers from packages installed under the shared ``extension_pkgs`` and
exposes the trigger point used by ``WebChannel``.

The runtime is deliberately independent from ``jiuwenswarm.extensions_v2``:
it only depends on ``openjiuwen.core.events`` and its own modules.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import shutil
from contextlib import nullcontext
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    ExtensionEventBus,
    HandlerLevel,
    PackageMetadata,
    SessionContext,
)

from openjiuwen.core.events.ops_impl.store import configure_store, unconfigure_store
from openjiuwen.core.extension.data import JsonStore
from openjiuwen.core.extension.data.json_values import valid_package_id

from jiuwenswarm.gateway.extensions_v2 import bootstrap
from jiuwenswarm.gateway.extensions_v2.builtin.router import GatewayLocalRouter, LocalRequest
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage, load_gateway_packages
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    EVENT_GATEWAY_DISCONNECT,
    GatewayDisconnectInput,
    GatewayRequestInput,
    GatewayRequestOutput,
    GatewayRoute,
    gateway_session_id,
)
from jiuwenswarm.gateway.extensions_v2.state import UiStateStore
from jiuwenswarm.gateway.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.gateway.extensions_v2.webui.runtime import GatewayWebUIRuntime
from jiuwenswarm.gateway.extensions_v2.webui.transport import GatewayTransport
from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserBridge

logger = logging.getLogger(__name__)

#: A frame must never be blocked by an extension for longer than this.
REQUEST_TIMEOUT_S = 5.0


class GatewayRuntime:
    """Process-wide event bus, package registry and frame trigger point."""

    def __init__(self, *, channel: Any, config: GatewayExtensionConfig) -> None:
        self.channel = channel
        self.config = config
        self.bus = ExtensionEventBus()
        self.store = UiStateStore()
        # Persistent package data, separate from the in-process UiStateStore above.
        self.data_store = JsonStore(Path(config.data_dir) / "store")
        self._data_store_bound = False
        self.router = GatewayLocalRouter()
        self.webui = GatewayWebUIRuntime(
            bus=self.bus,
            transport=GatewayTransport(channel, generation=lambda package_id: self.browser.frame_generation(package_id)),
        )
        self._handler_ids: list[str] = []
        self._builtin_ids: list[str] = []
        self._user_handlers: dict[str, int] = {}
        self._package_handlers: dict[str, list[tuple[str, str]]] = {}
        self._packages: list[LoadedPackage] = []
        self._management: Any = None
        self._started = False
        self.browser = BrowserBridge(self)

    @property
    def registry(self) -> ActiveRenderRegistry:
        """Active render instances owned by the WebUI runtime."""

        return self.webui.registry

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register events, run ``ops.build`` once and publish packages."""

        if self._started or not self.config.enabled:
            return
        bootstrap.register_event_types(self.bus, self)
        self._builtin_ids.extend(bootstrap.register_handlers(self.bus, self))
        self._bind_package_data()
        await self.bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
        for package in load_gateway_packages(self.config.install_dir):
            try:
                self.publish(package)
            except Exception as exc:  # noqa: BLE001 - one bad package must not block startup
                logger.warning(
                    "[GatewayExtensions] skip package %s at startup: %s",
                    package.package_id,
                    exc,
                )
        self._started = True
        logger.info(
            "[GatewayExtensions] started: packages=%d handlers=%d install_dir=%s",
            len(self._packages),
            self.user_handler_count,
            self.config.install_dir,
        )

    async def shutdown(self) -> None:
        """Unregister every handler and drop in-process state."""

        for handler_id in (*self._handler_ids, *self._builtin_ids):
            try:
                self.bus.unregister_event_handler(handler_id)
            except Exception as exc:  # noqa: BLE001 - shutdown must keep going
                logger.warning("[GatewayExtensions] failed to unregister %s: %s", handler_id, exc)
        self._handler_ids.clear()
        self._builtin_ids.clear()
        self._user_handlers.clear()
        self._package_handlers.clear()
        self._packages.clear()
        await self.browser.settle()
        await self.webui.shutdown()
        self.store.clear()
        if self._data_store_bound:
            unconfigure_store(self.bus)
            self._data_store_bound = False
        self.browser.connections.clear()
        self.browser.generations.clear()
        self.browser.appearance.clear()
        self._started = False

    @property
    def active(self) -> bool:
        """True when at least one USER handler may observe a frame."""

        return self._started and self.config.enabled and self.user_handler_count > 0

    @property
    def user_handler_count(self) -> int:
        """Number of published USER handlers; BUILTIN handlers never count."""

        return sum(self._user_handlers.values())

    @property
    def packages(self) -> Any:
        """Package management over the shared store (lazy, import-cycle free)."""

        if self._management is None:
            from jiuwenswarm.gateway.extensions_v2.management import GatewayPackageManager

            self._management = GatewayPackageManager(self, self.config.install_dir)
        return self._management

    async def connection_id(self, ws: Any) -> str:
        """Public connection id used by package-management frames."""

        return await self._connection_id(ws)

    # ── package publication ───────────────────────────────────────────────

    def publish(self, package: LoadedPackage) -> None:
        """Publish every declaration of one loaded package as a USER handler.

        Registration is all-or-nothing: when any declaration fails, the handlers
        registered so far are released and the failure is re-raised. Callers can
        therefore never report success for a half-registered package.
        """

        self.browser.validate_package(package)
        self.unpublish(package.package_id)
        metadata = PackageMetadata(
            package_id=package.package_id,
            package_version=package.version,
            root=package.root,
        )
        registered: list[tuple[str, str]] = []
        generation = uuid4().hex
        for declaration in package.handlers:
            try:
                handler_id = self.bus.register_event_handler(
                    declaration.event_type,
                    _async_handler(
                        declaration.handler,
                        browser=self.browser if self.browser.is_application_package(package) else None,
                        package_id=package.package_id,
                        generation=generation,
                    ),
                    declaration.match_pattern,
                    HandlerLevel.USER,
                    package_metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - roll the package back as a whole
                logger.warning(
                    "[GatewayExtensions] package %s cannot register %s: %s",
                    package.package_id,
                    declaration.event_type,
                    exc,
                )
                self._drop_handlers(registered)
                raise
            self._handler_ids.append(handler_id)
            registered.append((handler_id, declaration.event_type))
            self._user_handlers[declaration.event_type] = (
                self._user_handlers.get(declaration.event_type, 0) + 1
            )
        self._package_handlers[package.package_id] = registered
        self._packages.append(package)
        self.browser.published(package.package_id, generation)

    def unpublish(self, package_id: str) -> bool:
        """Release every handler of one package and drop its active record."""

        registered = self._package_handlers.pop(package_id, None)
        if registered is None:
            return False
        self._drop_handlers(registered)
        self._packages = [package for package in self._packages if package.package_id != package_id]
        self.browser.released(package_id)
        return True

    def _drop_handlers(self, registered: list[tuple[str, str]]) -> None:
        """Unregister ``(handler_id, event_type)`` pairs and fix the counters."""

        for handler_id, event_type in registered:
            try:
                self.bus.unregister_event_handler(handler_id)
            except Exception as exc:  # noqa: BLE001 - a stale id must not block the rest
                logger.warning("[GatewayExtensions] cannot unregister %s: %s", handler_id, exc)
            if handler_id in self._handler_ids:
                self._handler_ids.remove(handler_id)
            remaining = self._user_handlers.get(event_type, 0) - 1
            if remaining > 0:
                self._user_handlers[event_type] = remaining
            else:
                self._user_handlers.pop(event_type, None)

    # ── package store and data ────────────────────────────────────────────

    def _bind_package_data(self) -> None:
        """Bind the package store before ``ops.build``.

        The bus already carries agent-core's ``store.*`` events and the
        ``ops.store`` / ``ops.package`` namespaces; they need a bound store.
        """

        if not self._data_store_bound:
            configure_store(self.bus, self.data_store)
            self._data_store_bound = True

    def is_active_package(self, package_id: str) -> bool:
        """True when ``package_id`` is currently published."""

        return any(package.package_id == package_id for package in self._packages)

    def package_data_dir(self, package_id: str) -> Path:
        """Return ``<data_dir>/packages/<package_id>`` without creating it.

        Raises:
            ValueError: The id is invalid or a directory on the path is a symlink.
        """

        if not valid_package_id(package_id):
            raise ValueError("invalid package id")
        base = Path(self.config.data_dir)
        directory = base / "packages" / package_id
        for path in (base, base / "packages", directory):
            if path.is_symlink():
                raise ValueError("package data path must not be a symlink")
        return directory

    async def cleanup_package_data(self, package_id: str) -> None:
        """Delete the store and data directory of an uninstalled package."""

        await self.data_store.cleanup_package(package_id, reason="uninstall")
        directory = self.package_data_dir(package_id)
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)

    def active_packages(self) -> tuple[LoadedPackage, ...]:
        """Return the packages currently published on this runtime."""

        return tuple(self._packages)

    # ── trigger point: gateway.request ────────────────────────────────────

    async def intercept(
        self,
        ws: Any,
        req_id: str,
        method: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        *,
        request_context: Mapping[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Run one inbound frame through ``gateway.request``.

        ``headers`` mirrors ``request_context.headers`` of the browser frame:
        USER handlers validate credentials from it.

        Returns ``(visited, params)``. ``visited=True`` means the frame was
        answered locally and the native dispatch must be skipped; otherwise the
        returned params continue to the existing path unchanged.
        """

        if not self.active:
            return False, dict(params)
        connection_id = await self._connection_id(ws)
        if not connection_id:
            return False, dict(params)
        request = GatewayRequestInput(
            method=method,
            params=MappingProxyType(dict(params)),
            session_id=_session_of(params),
            connection_id=connection_id,
            user_id=self._user_id(ws),
            channel_id=self._channel_id(),
            headers=_normalized_headers(headers if headers is not None else (request_context or {}).get("headers")),
            request_context=MappingProxyType(dict(request_context or {})),
            request_id=req_id,
        )
        try:
            if self.browser.enabled:
                await self.browser.connect(ws, connection_id)
            result = await asyncio.wait_for(
                self.bus.trigger(
                    Event(EVENT_GATEWAY_REQUEST, request),
                    self._session_context(request),
                ),
                timeout=REQUEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[GatewayExtensions] gateway.request timed out after %.1fs: method=%s ws_id=%s",
                REQUEST_TIMEOUT_S,
                method,
                connection_id,
            )
            return False, dict(params)
        except Exception as exc:  # noqa: BLE001 - a broken chain must not break the frame
            logger.warning("[GatewayExtensions] gateway.request failed: method=%s err=%s", method, exc)
            return False, dict(params)
        return await self._apply(ws, req_id, request, result)

    async def _apply(
        self,
        ws: Any,
        req_id: str,
        request: GatewayRequestInput,
        result: object,
    ) -> tuple[bool, dict[str, Any]]:
        params = dict(request.params)
        if not isinstance(result, GatewayRequestOutput):
            return False, params
        route = GatewayRoute.parse(result.route)
        if route is None or route is GatewayRoute.FORWARD:
            if isinstance(result.params, Mapping):
                return False, dict(result.params)
            return False, params
        if route is GatewayRoute.CONSUME:
            await self._respond(ws, req_id, ok=True, payload=result.response)
            return True, params
        if route is GatewayRoute.DENY:
            await self._respond(ws, req_id, ok=False, error=result.error)
            return True, params
        await self._answer_locally(ws, req_id, request)
        return True, params

    async def _answer_locally(
        self,
        ws: Any,
        req_id: str,
        request: GatewayRequestInput,
    ) -> None:
        """Answer a ``local`` route through the Gateway builtin router."""

        response = await self.router.execute(
            LocalRequest(
                method=request.method,
                params=request.params,
                connection_id=request.connection_id,
                session_id=request.session_id,
                user_id=request.user_id,
                headers=request.headers,
            )
        )
        await self._respond(
            ws,
            req_id,
            ok=response.ok,
            payload=response.payload,
            error=None if response.ok else response.error,
        )

    async def _respond(
        self,
        ws: Any,
        req_id: str,
        *,
        ok: bool,
        payload: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        send = getattr(self.channel, "send_response", None)
        if not callable(send):
            return
        code: str | None = None
        message: str | None = None
        if isinstance(error, Mapping):
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code
            raw_message = error.get("message")
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
        await send(ws, req_id, ok=ok, payload=dict(payload or {}), error=message, code=code)

    # ── trigger point: disconnect ─────────────────────────────────────────

    async def on_disconnect(self, ws: Any, session_ids: Any = None) -> None:
        """Drop connection-scoped state when the browser socket closes."""

        del session_ids
        connection_id = str(getattr(ws, "_jiuwen_ws_id", "") or "")
        if not connection_id:
            return
        try:
            await asyncio.wait_for(self.bus.trigger(
                Event(EVENT_GATEWAY_DISCONNECT, GatewayDisconnectInput(connection_id)),
                SessionContext(gateway_session_id(connection_id), None, {"connection_id": connection_id}),
            ), timeout=REQUEST_TIMEOUT_S)
        finally:
            self.browser.drop_connection(connection_id)
            self.store.drop_connection(connection_id)
            await self.webui.drop_connection(connection_id)

    # ── helpers ───────────────────────────────────────────────────────────

    async def _connection_id(self, ws: Any) -> str:
        ensure = getattr(self.channel, "ensure_ws_id", None)
        if callable(ensure):
            try:
                return str(await ensure(ws) or "")
            except Exception as exc:  # noqa: BLE001 - fall back to the cached id
                logger.warning("[GatewayExtensions] cannot allocate a connection id: %s", exc)
        return str(getattr(ws, "_jiuwen_ws_id", "") or "")

    def _user_id(self, ws: Any) -> str | None:
        resolve = getattr(self.channel, "connection_user_id", None)
        if not callable(resolve):
            return None
        value = resolve(ws)
        text = str(value).strip() if value is not None else ""
        return text or None

    def _channel_id(self) -> str:
        return str(getattr(self.channel, "channel_id", "web") or "web")

    @staticmethod
    def _session_context(request: GatewayRequestInput) -> SessionContext:
        metadata: dict[str, object] = {
            "connection_id": request.connection_id,
            "channel_id": request.channel_id,
            "method": request.method,
            "request_id": request.request_id,
        }
        if request.user_id:
            metadata["user_id"] = request.user_id
        if request.session_id:
            metadata["session_id"] = request.session_id
        return SessionContext(gateway_session_id(request.connection_id), None, metadata)


def _session_of(params: Mapping[str, Any]) -> str | None:
    value = params.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalized_headers(headers: Mapping[str, str] | None) -> Mapping[str, str]:
    """Lower-case header names so lookups stay case-insensitive."""

    if not isinstance(headers, Mapping):
        return MappingProxyType({})
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if isinstance(name, str) and isinstance(value, str):
            normalized[name.strip().lower()] = value
    return MappingProxyType(normalized)


def _async_handler(
    handler: Callable[..., Any], *, browser: BrowserBridge | None = None,
    package_id: str = "", generation: str = "",
) -> Callable[..., Awaitable[Any]]:
    """Return a handler the event bus can await (same adapter as Core)."""

    if browser is None and inspect.iscoroutinefunction(handler):
        return handler

    @functools.wraps(handler)
    async def adapter(event: Any, ops: Any, event_next: Any) -> Any:
        scope = browser.caller_scope(package_id, generation) if browser is not None else nullcontext()
        with scope:
            result = handler(event, ops, event_next)
            if inspect.isawaitable(result):
                return await result
            return result

    return adapter
