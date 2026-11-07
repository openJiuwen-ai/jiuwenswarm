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
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    EventHandlerOps,
    ExtensionEventBus,
    HandlerLevel,
    PackageMetadata,
    SessionContext,
)

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.events.ops_impl.store import configure_store, store_call, unconfigure_store
from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.data import JsonStore
from openjiuwen.core.extension.data.json_values import valid_package_id

from jiuwenswarm.gateway.extensions_v2 import bootstrap
from jiuwenswarm.gateway.extensions_v2.builtin.router import GatewayLocalRouter, LocalRequest
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage, load_gateway_packages, release_package_modules
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
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
from jiuwenswarm.gateway.extensions_v2.webui.call_context import Caller, trusted_connection
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget
from jiuwenswarm.gateway.extensions_v2.webui.runtime import GatewayWebUIRuntime
from jiuwenswarm.gateway.extensions_v2.webui.transport import GatewayTransport
from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserBridge
from jiuwenswarm.gateway.extensions_v2.webui.host_settings import HOST_METHODS

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
            transport=GatewayTransport(channel, generation=lambda package_id, generation: self.browser.frame_generation(
                package_id, generation)),
            caller_for=self.caller_for,
        )
        self._handler_ids: list[str] = []
        self._builtin_ids: list[str] = []
        self._user_handlers: dict[str, int] = {}
        self._package_handlers: dict[str, list[tuple[str, str]]] = {}
        self._packages: list[LoadedPackage] = []
        self._activations: dict[str, _Activation] = {}
        # Which activation each handler's Ops belongs to; detached tasks keep using them.
        self._ops_owner: weakref.WeakKeyDictionary[Any, _Activation] = weakref.WeakKeyDictionary()
        # Handler and data calls in flight per package, across activations (S_01 invariant 12).
        self._inflight: dict[str, int] = {}
        # Prepared switches not yet committed or aborted (S_01 section 3.2); shutdown ends them.
        self._prepared: set[PreparedPackage] = set()
        # Set for good by shutdown: nothing more is prepared, and a runtime does not start twice.
        self._closed = False
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
        self.browser.splash_preferences.load()
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
        await self.browser.transfers.purge(package.package_id for package in self._packages)
        self._started = True
        logger.info(
            "[GatewayExtensions] started: packages=%d handlers=%d install_dir=%s",
            len(self._packages),
            self.user_handler_count,
            self.config.install_dir,
        )

    async def shutdown(self) -> None:
        """Unregister every handler and drop in-process state."""

        # No switch is prepared from here on, and one prepared earlier can no longer commit.
        self._closed = True
        for prepared in self._prepared:
            prepared.activation.release()
        self._prepared.clear()
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
        for activation in self._activations.values():
            activation.release()
        self._activations.clear()
        await self.browser.settle()
        await self.webui.shutdown()
        self.store.clear()
        if self._data_store_bound:
            unconfigure_store(self.bus)
            self._data_store_bound = False
        self.browser.connections.clear()
        self.browser.appearance.clear()
        self.browser.transfers.clear()
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
        """Publish one loaded package, replacing its running version.

        Equivalent to :meth:`prepare` followed by :meth:`commit`; callers that must
        act between the two (management closes the running splash) use them directly.
        """

        prepared = self.prepare(package)
        try:
            self.commit(prepared)
        except BaseException:
            self.abort(prepared)
            raise

    def prepare(self, package: LoadedPackage) -> PreparedPackage:
        """Validate a package and check that every handler can be registered, changing nothing.

        The check registers a placeholder for each handler and removes the batch again in the
        same synchronous step, so no event chain can match it and no package code runs. The
        caller owns the result: it either commits or aborts it, and shutdown ends it.

        Raises:
            ExtensionFailure: The runtime has shut down.
            Exception: Validation or registration failed; the package's modules are released.
        """

        try:
            if self._closed:
                raise ExtensionFailure(StatusCode.EXTENSION_LOAD_FAILED, reason="the runtime has shut down")
            self.browser.validate_package(package)
            self._drop_handlers(self._register_batch(package, lambda declaration: _pass_on))
        except Exception:
            release_package_modules(package)
            raise
        activation = _Activation(package, uuid4().hex, application=self.browser.is_application_package(package),
                                 ops_owner=self._ops_owner, inflight=self._inflight)
        prepared = PreparedPackage(package, activation)
        self._prepared.add(prepared)
        return prepared

    def commit(self, prepared: PreparedPackage) -> None:
        """Replace the running version by a prepared one in one synchronous step.

        The new batch is registered before the old one is dropped, with no await in between:
        a chain matched before this step holds only the old handlers, one matched after it
        only the new ones (agent-core ``replace_published``).

        Raises:
            ExtensionFailure: The switch was aborted or the runtime shut down after it was prepared.
            Exception: Registering the new batch failed; the running version is untouched.
            The caller aborts the prepared package in both cases.
        """

        if prepared not in self._prepared:
            raise ExtensionFailure(
                StatusCode.EXTENSION_LOAD_FAILED, reason="the prepared switch ended before it was committed",
            )
        package, activation = prepared.package, prepared.activation
        registered = self._register_batch(package, lambda declaration: _async_handler(declaration.handler, activation))
        self._prepared.discard(prepared)
        self.unpublish(package.package_id)
        self._package_handlers[package.package_id] = registered
        self._activations[package.package_id] = activation
        self._packages.append(package)
        self.browser.published(package.package_id)

    def abort(self, prepared: PreparedPackage) -> None:
        """Drop a prepared package that will not be committed; the running version is untouched."""

        self._prepared.discard(prepared)
        prepared.activation.release()

    def _register_batch(self, package: LoadedPackage,
                        handler_for: Callable[[HandlerDeclaration], Callable[..., Any]]) -> list[tuple[str, str]]:
        """Register one handler per declaration, all or nothing.

        Raises:
            Exception: A registration failed; the handlers registered so far are dropped.
        """

        metadata = PackageMetadata(
            package_id=package.package_id,
            package_version=package.version,
            root=package.root,
        )
        registered: list[tuple[str, str]] = []
        for declaration in package.handlers:
            try:
                handler_id = self.bus.register_event_handler(
                    declaration.event_type,
                    handler_for(declaration),
                    declaration.match_pattern,
                    HandlerLevel.USER,
                    package_metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - roll the batch back as a whole
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
        return registered

    def unpublish(self, package_id: str) -> bool:
        """Release every handler of one package and drop its active record."""

        registered = self._package_handlers.pop(package_id, None)
        if registered is None:
            return False
        self._drop_handlers(registered)
        self._packages = [package for package in self._packages if package.package_id != package_id]
        activation = self._activations.pop(package_id, None)
        if activation is not None:
            activation.release()
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

    def current_activation(self, package_id: str) -> _Activation | None:
        """The published activation of ``package_id``, the one source of activation identity."""

        return self._activations.get(package_id)

    def current_activations(self) -> tuple[_Activation, ...]:
        """Every published activation."""

        return tuple(self._activations.values())

    def caller_for(self, ops: EventHandlerOps | None) -> Caller | None:
        """Resolve the activation that issued ``ops`` (also from a detached task) as a host caller."""

        activation = self._ops_owner.get(ops) if ops is not None else None
        if activation is None:
            return None
        connection_id = trusted_connection(ops)

        def current(target: RenderTarget | None = None) -> bool:
            if self._activations.get(activation.package_id) is not activation:
                return False
            if target is None:
                return True
            lookup = getattr(self.channel, "ws_by_id", None)
            ws = lookup(connection_id) if callable(lookup) else None
            return (target.package_id == activation.package_id and bool(connection_id)
                    and (not target.connection_id or target.connection_id == connection_id)
                    and ws is not None and not getattr(ws, "closed", False))

        return Caller(activation.package_id, activation.generation, connection_id, current)

    def _bind_package_data(self) -> None:
        """Bind the package store before ``ops.build``.

        The bus already carries agent-core's ``store.*`` events and the
        ``ops.store`` / ``ops.package`` namespaces; they need a bound store.
        """

        if not self._data_store_bound:
            configure_store(
                self.bus, self.data_store, begin_calls=self._begin_data_calls, end_calls=self._end_data_calls,
            )
            self._data_store_bound = True

    def _begin_data_calls(self, package_ids: Sequence[str], ops: Any = None) -> None:
        """Admit one data call per package from Ops of its current activation (S_01 invariant 12).

        Raises:
            ExtensionFailure: The Ops belong to a released activation or another package.
        """

        owner = self._ops_owner.get(ops) if ops is not None else None
        for package_id in package_ids:
            if owner is None or self._activations.get(package_id) is not owner:
                raise ExtensionFailure(
                    StatusCode.EVENTS_OPS_INVALID, reason="the package activation that issued these ops has ended",
                )
        for package_id in package_ids:
            _count(self._inflight, package_id, 1)

    def _end_data_calls(self, package_ids: Sequence[str]) -> None:
        """Finish the data calls admitted by :meth:`_begin_data_calls`."""

        for package_id in package_ids:
            _count(self._inflight, package_id, -1)

    @contextmanager
    def package_data_call(self, package_id: str, ops: Any) -> Iterator[None]:
        """Count a Gateway data operation such as ``ops.data.root()`` like a store call."""

        with store_call(self.bus, package_id, ops):
            yield

    @contextmanager
    def package_io_call(self, package_id: str, generation: str) -> Iterator[None]:
        """Keep host-mediated file work busy through cleanup, using its validated activation."""
        activation = self._activations.get(package_id)
        if activation is None or activation.generation != generation:
            raise ExtensionFailure(StatusCode.EVENTS_OPS_INVALID, reason="the file operation's activation has ended")
        _count(self._inflight, package_id, 1)
        try:
            yield
        finally:
            self._end_data_calls((package_id,))

    def stop_package(self, package_id: str) -> bool:
        """Unpublish a package that is about to be uninstalled.

        Raises:
            ExtensionFailure: ``EXTENSION_PACKAGE_BUSY`` while a handler or data
                call of the package is still running; nothing is unregistered.
        """

        self.require_idle(package_id)
        return self.unpublish(package_id)

    def require_idle(self, package_id: str) -> None:
        """Raise ``EXTENSION_PACKAGE_BUSY`` while the package has calls in flight.

        Handlers of an activation that a reload already replaced still count.
        """

        if self._inflight.get(package_id, 0):
            raise ExtensionFailure(
                StatusCode.EXTENSION_PACKAGE_BUSY, package_id=package_id,
                reason="in-flight calls remain; uninstall stops without unregistering",
            )

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

        # This package's ticket directories are deleted in the background; let those finish first.
        await self.browser.transfers.settle_package(package_id)
        await self.data_store.cleanup_package(package_id, reason="uninstall")
        directory = self.package_data_dir(package_id)
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)
        # Host settings about the package share its data lifecycle (S_10 section 4).
        self.browser.splash_preferences.forget(package_id)

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

        platform = self._started and self.browser.is_platform_frame(method, params)
        # Host methods are answered by the Gateway even without USER handlers (S_10 section 3).
        host = self._started and method in HOST_METHODS
        if not self.active and not platform and not host:
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
        if platform:
            # Host lifecycle frames carry no user input and bypass request policy (S_09).
            payload = await self.browser.platform_frame(request, self._session_context(request))
            await self._respond(ws, req_id, ok=True, payload=payload)
            return True, dict(params)
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


@dataclass(frozen=True, eq=False)
class PreparedPackage:
    """A validated package whose handlers can be registered (see :meth:`GatewayRuntime.prepare`).

    Identity-compared; the runtime that prepared it commits it at most once, and shutdown ends it.
    """

    package: LoadedPackage
    activation: _Activation


def _count(counter: dict[str, int], key: str, delta: int) -> None:
    remaining = counter.get(key, 0) + delta
    if remaining > 0:
        counter[key] = remaining
    else:
        counter.pop(key, None)


class _Activation:
    """One publication of a package: its generation, Ops and in-flight handler calls.

    It is the only record of activation identity: the browser bridge reads generations
    from it, and Ops handed to its handlers stay tied to it, so data and host calls stop
    once it is released (S_01 invariant 12). Its modules leave ``sys.modules`` once it is
    released and none of its handlers still runs (invariant 11).
    """

    def __init__(self, package: LoadedPackage, generation: str, *, application: bool,
                 ops_owner: weakref.WeakKeyDictionary[Any, _Activation], inflight: dict[str, int]) -> None:
        self.package = package
        self.package_id = package.package_id
        self.generation = generation
        self.application = application
        self._ops_owner = ops_owner
        self._inflight = inflight
        self._running = 0
        self._released = False
        self._purged = False

    @property
    def released(self) -> bool:
        return self._released

    def enter(self, ops: Any) -> None:
        self._ops_owner[ops] = self
        self._running += 1
        _count(self._inflight, self.package_id, 1)

    def leave(self) -> None:
        self._running -= 1
        _count(self._inflight, self.package_id, -1)
        self._purge_when_idle()

    def release(self) -> None:
        self._released = True
        self._purge_when_idle()

    def _purge_when_idle(self) -> None:
        if self._released and self._running == 0 and not self._purged:
            self._purged = True
            release_package_modules(self.package)


async def _pass_on(event: Any, ops: Any, event_next: Any) -> Any:
    """Placeholder registered by :meth:`GatewayRuntime.prepare` to check a handler can be registered."""

    del ops
    return await event_next(event)


def _async_handler(handler: Callable[..., Any], activation: _Activation) -> Callable[..., Awaitable[Any]]:
    """Return a handler the event bus can await, counted against its activation."""

    @functools.wraps(handler)
    async def adapter(event: Any, ops: Any, event_next: Any) -> Any:
        if activation.released:
            # A chain matched before the release still holds this handler: pass the event on instead.
            return await event_next(event)
        activation.enter(ops)
        try:
            result = handler(event, ops, event_next)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            activation.leave()

    return adapter
