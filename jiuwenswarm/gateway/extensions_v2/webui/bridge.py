# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application UI and browser commands for the local Gateway integration."""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from openjiuwen.core.events import Event, EventHandlerOps, EventHandlerOpsBuilder, SessionContext

from jiuwenswarm.gateway.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_UI_APPEARANCE, EVENT_UI_DOWNLOAD, EVENT_UI_SPLASH_PREVIEW,
    GatewayRequestInput, GatewayRequestOutput, WEBUI_DISPATCH_METHOD, gateway_session_id,
)
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    HostActionOutput, RenderNode, RenderTarget, UiActionInput, UiActionOutput, UiAppearanceInput,
    UiDownloadInput, UiSplashPreviewInput, valid_instance_key,
)
from jiuwenswarm.gateway.extensions_v2.webui.call_context import Caller
from jiuwenswarm.gateway.extensions_v2.webui.constants import UPSTREAM_EVENTS, WebUIEvent
from jiuwenswarm.gateway.extensions_v2.webui.component_rules import validate_event_data
from jiuwenswarm.gateway.extensions_v2.webui.appearance import AppearanceProviders, validate_appearance
from jiuwenswarm.gateway.extensions_v2.webui.host_settings import SplashPreferences
from jiuwenswarm.gateway.extensions_v2.webui.settings_contributions import SETTINGS_SLOT, package_settings
from jiuwenswarm.gateway.extensions_v2.webui.transfers import TransferManager

SLOTS = frozenset({"application.entry", "application.sidebar.footer", "application.background", "application.splash",
                   SETTINGS_SLOT})
SPLASH_SLOT = "application.splash"
#: Display-only slots whose lifecycle frames bypass request policy (S_09 section 3).
PLATFORM_SLOTS = frozenset({"application.background", SPLASH_SLOT})
SPLASH_PROVIDERS_METHOD = "webui.splash.providers"
SPLASH_READY_FIELDS = frozenset({"launch_id", "first_visit", "reduced_motion", "preview"})
CLOSE_REASONS = frozenset({"complete", "skip", "timeout", "error", "disabled", "removed"})
MIN_SPLASH_MS = 1_000
MAX_SPLASH_MS = 60_000
#: Package handlers answering a platform frame get the same budget as gateway.request.
PLATFORM_TIMEOUT_S = 5.0
ASSET_PREFIX = "/extension-assets/"
#: Marks a generation segment that addresses the package data ``public/`` directory.
DATA_GENERATION_SUFFIX = ".data"
ASSET_SOURCES = frozenset({"package", "data"})
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~\da-z-]+$", re.IGNORECASE | re.ASCII)
FORBIDDEN = frozenset({"__proto__", "prototype", "constructor"})
logger = logging.getLogger(__name__)


def json_copy(value: Any, *, maximum: int = 96 * 1024, depth: int = 16) -> Any:
    """Validate bounded, finite JSON and isolate it from its producer."""
    def visit(item: Any, level: int) -> None:
        if level > depth:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(item, dict):
            if any(not isinstance(key, str) or key in FORBIDDEN for key in item):
                raise ValueError("invalid object key")
            for member in item.values():
                visit(member, level + 1)
        elif isinstance(item, list):
            for member in item:
                visit(member, level + 1)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("value is not JSON")
    visit(value, 0)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("JSON byte limit exceeded")
    return json.loads(encoded)


def tree_nodes(root: RenderNode) -> dict[str, RenderNode]:
    """Index the authoritative render snapshot using browser node paths."""
    nodes: dict[str, RenderNode] = {}

    def walk(node: RenderNode, key: str) -> None:
        nodes[key] = node
        for index, child in enumerate(node.children):
            walk(child, f"{key}.{index}")
        field = "children" if node.component == "Tabs" else "label" if node.component == "Dropdown" else ""
        if field:
            for index, item in enumerate(node.props.get("items", ())):
                content = item.get(field, ())
                if isinstance(content, (list, tuple)):
                    for child_index, child in enumerate(content):
                        walk(child, f"{key}.items.{index}.{field}.{child_index}")

    walk(root, "0")
    return nodes


@dataclass
class BrowserConnection:
    """State owned by one live browser socket."""

    ws: Any
    epoch: str = field(default_factory=lambda: uuid4().hex)
    declaration_signature: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserBridge:
    """Bind package operations to trusted connection and package identities."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.connections: dict[str, BrowserConnection] = {}
        self.appearance = AppearanceProviders()
        self._appearance_locks: dict[str, asyncio.Lock] = {}
        # Browser notifications started from synchronous package release.
        self._tasks: set[asyncio.Task] = set()
        self.splash_preferences = SplashPreferences(runtime.config.data_dir)
        self.transfers = TransferManager(runtime)
        self.gateway_id = hashlib.sha256(str(runtime.config.install_dir.resolve()).encode()).hexdigest()

    @property
    def enabled(self) -> bool:
        """Whether a client-capability package needs declarations and HTTP interception."""
        return any(package.client for package in self.runtime.active_packages())

    @property
    def application_enabled(self) -> bool:
        """Whether any active package contributes to a Gateway application slot."""
        return any(activation.application for activation in self.runtime.current_activations())

    @property
    def generations(self) -> Mapping[str, str]:
        """Activation generation of each published application package (read-only).

        Derived from the runtime's activations on every read, so it can never disagree with them.
        """
        return MappingProxyType({activation.package_id: activation.generation
                                 for activation in self.runtime.current_activations() if activation.application})

    @staticmethod
    def is_application_package(package) -> bool:
        """Return True for packages contributing to a Gateway application slot.

        Application UI does not depend on request bindings: a package that only
        declares slot contributions still owns its interactions and assets.
        """
        items = package.ui.get("items", []) if isinstance(package.ui, Mapping) else []
        return isinstance(items, list) and any(
            isinstance(item, Mapping) and item.get("type") == "web.slot.contribution" and item.get("slot") in SLOTS
            for item in items
        )

    def application_packages(self) -> list:
        """Return the active packages that own Gateway application UI."""
        return [activation.package for activation in self.runtime.current_activations() if activation.application]

    def active_application_package(self, package_id: str):
        """Return the active application package with this id, or None."""
        activation = self.runtime.current_activation(package_id)
        return activation.package if activation is not None and activation.application else None

    def package_title(self, package_id: str) -> dict[str, str]:
        """Navigation title as ``{zh, en}``: manifest ``display.name``, else the package id."""
        package = self.active_application_package(package_id)
        names = getattr(package, "display_name", None) if package is not None else None
        if isinstance(names, Mapping) and names.get("zh") and names.get("en"):
            return {"zh": names["zh"], "en": names["en"]}
        return {"zh": package_id, "en": package_id}

    def settings_contributions(self) -> list[dict[str, Any]]:
        """Settings declarations of active Gateway packages, as ``webui.settings.catalog`` items (S_18, S_10 section 2)."""
        return [{"package_id": package.package_id, "id": item_id, **settings}
                for package in self.application_packages()
                for item_id, settings in package_settings(package.ui)]

    @staticmethod
    def _settings_instance(package, instance_key: str) -> bool:
        """Whether the package declared a settings item with this instance key."""
        return any(settings["kind"] == "item" and settings["instance_key"] == instance_key
                   for _, settings in package_settings(package.ui))

    def validate_package(self, package) -> None:
        """Validate browser declarations before publication."""
        package_settings(package.ui)
        splash = self.splash_declaration(package)
        if splash is not None and splash["declared_max_duration_ms"] > MAX_SPLASH_MS:
            logger.warning("[GatewayExtensions] %s declares a %d ms splash; it runs for at most %d ms",
                           package.package_id, splash["declared_max_duration_ms"], MAX_SPLASH_MS)
        if not package.client:
            return
        if set(package.client) != {"requestBindings"}:
            raise ValueError("client only accepts requestBindings")
        bindings = package.client["requestBindings"]
        if not isinstance(bindings, list):
            raise ValueError("requestBindings must be a list")
        targets: list[tuple[str, list[str]]] = []
        for active in self.runtime.active_packages():
            if active.package_id != package.package_id:
                for binding in active.client.get("requestBindings", []):
                    target = binding["target"]
                    targets.append((binding["type"], [target["name"].lower()] if binding["type"] == "http" else target["path"]))
        for binding in json_copy(bindings):
            if not isinstance(binding, dict) or set(binding) != {"type", "source", "target"}:
                raise ValueError("invalid request binding")
            source, target = binding["source"], binding["target"]
            if not isinstance(source, dict) or set(source) - {"key", "path"}:
                raise ValueError("invalid binding source")
            self._storage_key(source.get("key"))
            self._path(source.get("path", []))
            if not isinstance(target, dict) or not isinstance(target.get("prefix", ""), str):
                raise ValueError("invalid binding target")
            if binding["type"] == "http":
                name = target.get("name")
                if (set(target) - {"location", "name", "prefix"} or target.get("location") != "header"
                        or not isinstance(name, str) or not HEADER_NAME.fullmatch(name)):
                    raise ValueError("invalid HTTP binding")
                parts = [name.lower()]
            elif binding["type"] == "websocket":
                parts = target.get("path")
                self._path(parts)
                if (set(target) - {"location", "path", "prefix"} or target.get("location") != "message"
                        or len(parts) < 2 or parts[0] != "request_context"):
                    raise ValueError("invalid WebSocket binding")
            else:
                raise ValueError("unsupported binding transport")
            for transport, existing in targets:
                length = min(len(parts), len(existing))
                if transport == binding["type"] and parts[:length] == existing[:length]:
                    raise ValueError("request binding targets overlap")
            targets.append((binding["type"], parts))
        items = package.ui.get("items", [])
        if package.ui.get("apiVersion") != 1 or not isinstance(items, list) or not items:
            raise ValueError("browser package needs ui.apiVersion and contributions")
        if any(not isinstance(item, dict) or item.get("type") != "web.slot.contribution"
               or item.get("slot") not in SLOTS for item in items):
            raise ValueError("unsupported local application contribution")

    @staticmethod
    def splash_declaration(package) -> dict[str, Any] | None:
        """Read the package's ``application.splash`` declaration (S_09 section 1)."""
        ui = package.ui if isinstance(package.ui, Mapping) else {}
        items = ui.get("items", [])
        declaration = None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            if "splash" in item and item.get("slot") != SPLASH_SLOT:
                raise ValueError("splash settings only belong to application.splash")
            if item.get("type") != "web.slot.contribution" or item.get("slot") != SPLASH_SLOT:
                continue
            settings = item.get("splash", {})
            if not isinstance(settings, Mapping) or set(settings) - {"skippable", "maxDurationMs"}:
                raise ValueError("splash accepts skippable and maxDurationMs")
            skippable = settings.get("skippable", False)
            maximum = settings.get("maxDurationMs", MAX_SPLASH_MS)
            if not isinstance(skippable, bool):
                raise ValueError("splash.skippable must be a boolean")
            if type(maximum) is not int or maximum < MIN_SPLASH_MS:
                raise ValueError(f"splash.maxDurationMs must be an integer of at least {MIN_SPLASH_MS}")
            declaration = {"skippable": skippable, "max_duration_ms": min(maximum, MAX_SPLASH_MS),
                           "declared_max_duration_ms": maximum}
        return declaration

    def splash_providers(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Active splash providers in effective order; disabled ones only on request (S_10 section 4)."""
        declared = {}
        for package in self.application_packages():
            declaration = self.splash_declaration(package)
            if declaration is not None:
                declared[package.package_id] = {"package_id": package.package_id, "skippable": declaration["skippable"],
                                                "max_duration_ms": declaration["max_duration_ms"]}
        preferences = self.splash_preferences
        return [declared[package_id] for package_id in preferences.ordered(declared)
                if include_disabled or preferences.enabled(package_id)]

    @staticmethod
    def is_platform_frame(method: str, params: Any) -> bool:
        """Whether a frame is a host lifecycle frame that bypasses request policy."""
        if method == SPLASH_PROVIDERS_METHOD:
            return True
        if method != WEBUI_DISPATCH_METHOD or not isinstance(params, Mapping):
            return False
        target = params.get("target")
        return (isinstance(target, Mapping) and target.get("scope") == "application"
                and target.get("slot_id") in PLATFORM_SLOTS
                and params.get("event") in (WebUIEvent.READY.value, WebUIEvent.CLOSED.value))

    async def platform_frame(self, request, context: SessionContext) -> dict[str, Any]:
        """Answer one platform frame; package handlers share the request time budget."""
        if request.method == SPLASH_PROVIDERS_METHOD:
            return {"providers": self.splash_providers(), "customized": self.splash_preferences.customized}
        try:
            receipt = await asyncio.wait_for(self._lifecycle(request, context), timeout=PLATFORM_TIMEOUT_S)
        except asyncio.TimeoutError:
            receipt = UiActionOutput(accepted=False, status="timeout")
        response = {"accepted": receipt.accepted, "status": receipt.status}
        if receipt.code:
            response["code"] = receipt.code
        return response

    async def _lifecycle(self, request, context: SessionContext) -> UiActionOutput:
        params = request.params
        target = params["target"]
        event_name = params.get("event")
        package_id = target.get("package_id", "")
        invalid = UiActionOutput(accepted=False, status="invalid_target", code="SCHEMA_ERROR")
        if target.get("slot_id") != SPLASH_SLOT:
            if event_name != WebUIEvent.READY.value:
                return invalid
            return await self.dispatch(request, context) or UiActionOutput(accepted=False, status="unhandled")
        if not isinstance(package_id, str) or not package_id:
            return invalid
        instance_key = target.get("instance_key", "")
        if not valid_instance_key(instance_key):
            return invalid
        scoped = RenderTarget(session_id="", slot_id=SPLASH_SLOT, package_id=package_id, instance_key=instance_key,
                              scope="application", connection_id=request.connection_id)
        payload = params.get("payload", {})
        if event_name == WebUIEvent.READY.value:
            launch_id = payload.get("launch_id") if isinstance(payload, Mapping) else None
            if (not isinstance(payload, Mapping) or set(payload) != SPLASH_READY_FIELDS
                    or not isinstance(launch_id, str) or not 0 < len(launch_id) <= 128
                    or any(not isinstance(payload[name], bool) for name in ("first_visit", "reduced_motion", "preview"))):
                return UiActionOutput(accepted=False, status="invalid_payload", code="SCHEMA_ERROR")
            receipt = await self.dispatch(request, context)
            if receipt is None or not receipt.accepted:
                return UiActionOutput(accepted=False, status="unhandled")
            record = self.runtime.registry.get(scoped)
            return UiActionOutput(accepted=True, status="playing" if record and record.root is not None else "skipped")
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        revision = payload.get("revision") if isinstance(payload, Mapping) else None
        if set(payload) != {"reason", "revision"} or reason not in CLOSE_REASONS or type(revision) is not int:
            return UiActionOutput(accepted=False, status="invalid_payload", code="SCHEMA_ERROR")
        record = self.runtime.registry.get(scoped)
        if (self.active_application_package(package_id) is None or record is None or record.root is None
                or record.revision != revision):
            return UiActionOutput(accepted=False, status="inactive", code="TARGET_INACTIVE")
        # Drop first so a repeated or concurrent close is answered as inactive.
        self.runtime.registry.drop_instance(scoped)
        await self.runtime.bus.trigger(Event(WebUIEvent.CLOSED.value, UiActionInput(
            WebUIEvent.CLOSED.value, scoped, MappingProxyType({"reason": reason}), request.request_context,
        )), context)
        return UiActionOutput(accepted=True, status="closed")

    def published(self, package_id: str) -> None:
        package = self.active_application_package(package_id)
        if package is not None and self.splash_declaration(package) is not None:
            self.splash_preferences.note_installed(package_id)

    async def close_splashes(self, package_id: str, reason: str) -> int:
        """Close a package's live splash instances and tell it why (S_10 section 5).

        The browser closes first through the clear frame; the package hears
        ``ui.closed`` afterwards and is never waited on beyond the platform limit.
        """
        closed = 0
        for instance in self.runtime.registry.snapshot():
            target = instance.target
            if target.slot_id != SPLASH_SLOT or target.package_id != package_id or instance.root is None:
                continue
            frame = self.runtime.registry.retire_instance(target, reason)
            if frame is None:
                continue
            await self.runtime.webui.transport.send_clear(frame)
            context = SessionContext(gateway_session_id(target.connection_id), None,
                                     {"connection_id": target.connection_id, "channel_id": "web"})
            try:
                await asyncio.wait_for(self.runtime.bus.trigger(Event(WebUIEvent.CLOSED.value, UiActionInput(
                    WebUIEvent.CLOSED.value, target, MappingProxyType({"reason": reason}),
                )), context), timeout=PLATFORM_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - the host has already closed the splash
                logger.warning("[GatewayExtensions] ui.closed(%s) for %s failed: %s", reason, package_id, exc)
            closed += 1
        return closed

    def frame_generation(self, package_id: str, generation: str | None = None) -> str | None:
        """The generation a frame carries: the calling activation's, else the current one.

        Frames a package's render caused stay attributed to the activation that called it; frames the
        host sends on its own (release, eviction, shutdown) carry the current one. Only client-capability
        packages are declared to the browser, so frames of other application packages carry none.
        """
        activation = self.runtime.current_activation(package_id)
        if activation is None or not activation.package.client:
            return None
        return generation or activation.generation

    def released(self, package_id: str) -> None:
        """Retire a package's views and appearance, and tell the affected browsers."""
        clears = self.runtime.registry.retire_package(package_id)
        revokes = self.appearance.release(package_id)
        if clears or revokes:
            self.spawn(self._notify_release(package_id, clears, revokes))
        for upload in self.transfers.release(package_id=package_id):
            self.transfers.discard_later(upload)

    async def _notify_release(self, package_id: str, clears, revokes) -> None:
        transport = self.runtime.webui.transport
        for frame in clears:
            await transport.send_clear(frame)
        for connection_id, revision in revokes:
            # The connection lock orders the revoke after a frame that was still in flight.
            async with self._appearance_locks.setdefault(connection_id, asyncio.Lock()):
                await transport.send_appearance(connection_id, {
                    "package_id": package_id, "revision": revision, "appearance": None,
                })

    def spawn(self, coroutine) -> asyncio.Task | None:
        """Run a browser notification or file cleanup in the background; ``settle`` waits for it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Without a loop no browser can be connected either.
            coroutine.close()
            return None
        task = loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def settle(self) -> None:
        """Wait until release notifications have been sent."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def apply_appearance(self, caller: Caller, appearance: Mapping[str, Any] | None) -> dict[str, Any]:
        """Deliver or revoke the calling package's appearance on its browser connection (S_08)."""
        package = self._action_package(caller)
        connection_id = caller.connection_id
        if not connection_id:
            raise ValueError("ops.appearance requires a browser connection")
        normalized = None if appearance is None else validate_appearance(appearance)
        lock = self._appearance_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            # A release while this call waited reserved its revoke already; a stale
            # activation must not send a later revision that would outrank it.
            self._action_package(caller)
            if normalized is None and self.appearance.provider(connection_id) != package.package_id:
                return {"status": "unchanged", "revision": self.appearance.revision(connection_id)}
            revision = self.appearance.next_revision(connection_id)
            self.appearance.begin_send(connection_id, package.package_id)
            try:
                delivered = await self.runtime.webui.transport.send_appearance(connection_id, {
                    "package_id": package.package_id, "revision": revision, "appearance": normalized,
                })
            finally:
                still_owned = self.appearance.finish_send(connection_id, package.package_id)
            if not delivered:
                return {"status": "delivery_failed", "revision": revision}
            # A release during delivery queued a later revoke; keep it authoritative. Shutdown ends
            # activations without releasing in-flight records, so currency is checked as well.
            if still_owned and caller.current():
                self.appearance.set(connection_id, package.package_id if normalized else None)
            return {"status": "applied" if normalized else "revoked", "revision": revision}

    async def connect(self, ws: Any, connection_id: str) -> None:
        """Deliver trusted declarations before processing a business frame."""
        connection = self.connections.setdefault(connection_id, BrowserConnection(ws))
        async with connection.lock:
            await self._declare(connection)

    async def _declare(self, connection: BrowserConnection) -> None:
        packages = [{"packageId": activation.package_id, "activationGeneration": activation.generation,
                     "requestBindings": activation.package.client["requestBindings"]}
                    for activation in self.runtime.current_activations() if activation.package.client]
        signature = json.dumps(packages, sort_keys=True)
        if signature != connection.declaration_signature:
            await self.runtime.channel.send_event(connection.ws, "client.declarations", {
                "clientProtocolVersion": 1, "gatewayInstanceId": self.gateway_id,
                "connectionEpoch": connection.epoch, "packages": packages,
            })
            connection.declaration_signature = signature

    def drop_connection(self, connection_id: str) -> None:
        self.connections.pop(connection_id, None)
        self.appearance.drop_connection(connection_id)
        self._appearance_locks.pop(connection_id, None)
        for upload in self.transfers.release(connection_id=connection_id):
            self.transfers.discard_later(upload)

    def register_events(self, bus) -> None:
        async def core(event, ops, next_handler):
            del ops, next_handler
            if not isinstance(event.event_input, UiActionInput):
                raise TypeError("UI events require UiActionInput")
            return UiActionOutput(accepted=False, status="unhandled")
        for event_name in sorted(UPSTREAM_EVENTS | {WebUIEvent.CLOSED.value}):
            bus.register_event_type(event_name, match_pattern_subset, core)

    def build_request_dispatch_handler(self):
        """Dispatch application UI only after the USER request policy continues."""
        async def dispatch_request(event, ops, next_handler):
            request = event.event_input
            if (not self.application_enabled or not isinstance(request, GatewayRequestInput)
                    or request.transport != "websocket" or request.method != WEBUI_DISPATCH_METHOD):
                return await next_handler(event)
            receipt = await self.dispatch(request, ops.session_context)
            if receipt is None:
                return await next_handler(event)
            response = {"accepted": receipt.accepted, "status": receipt.status}
            if receipt.code:
                response["code"] = receipt.code
            return GatewayRequestOutput(handled=True, route="consume", response=response)
        return dispatch_request

    def build_ops_handler(self):
        async def asset_url(ops, path: str, *, source: str = "package"):
            if source not in ASSET_SOURCES:
                raise ValueError("asset source must be 'package' or 'data'")
            package, caller = self._package_caller(ops)
            generation = caller.generation
            if source == "data":
                generation += DATA_GENERATION_SUFFIX
            self.asset_file(package.package_id, generation, path)
            return f"{ASSET_PREFIX}{package.package_id}/{generation}/{path}"
        async def storage_set(ops, key: str, value: Any):
            return await self.operation(ops, "local_storage.set", {"key": key, "value": value})
        async def storage_delete(ops, key: str):
            return await self.operation(ops, "local_storage.delete", {"key": key})
        async def page_reload(ops):
            return await self.operation(ops, "page.reload", {})
        # Host actions start their own event chain; the CORE default does the work (S_01 section 4).
        async def appearance_apply(ops, appearance: Mapping[str, Any] | None = None):
            return await self._run_action(ops, EVENT_UI_APPEARANCE,
                                          lambda package_id: UiAppearanceInput(package_id, appearance))
        async def download(ops, path: str, *, filename: str | None = None):
            return await self._run_action(ops, EVENT_UI_DOWNLOAD,
                                          lambda package_id: UiDownloadInput(package_id, path, filename))
        async def splash_preview(ops):
            return await self._run_action(ops, EVENT_UI_SPLASH_PREVIEW, UiSplashPreviewInput)
        async def build(event, ops, next_handler):
            del ops
            builder = await next_handler(event)
            if not isinstance(builder, EventHandlerOpsBuilder):
                raise TypeError("ops.build must return EventHandlerOpsBuilder")
            builder.add_method("ui", "asset_url", asset_url)
            builder.add_method("ui", "download", download)
            builder.add_namespace("client")
            builder.add_method("client", "local_storage_set", storage_set)
            builder.add_method("client", "local_storage_delete", storage_delete)
            builder.add_method("client", "page_reload", page_reload)
            builder.add_namespace("appearance")
            builder.add_method("appearance", "apply", appearance_apply)
            builder.add_namespace("splash")
            builder.add_method("splash", "preview", splash_preview)
            return builder
        return build

    async def _run_action(self, ops: EventHandlerOps, event_name: str, make_input) -> dict[str, Any]:
        """Trigger one host action chain for the calling activation and translate its answer.

        The caller is resolved from ``ops`` here and passed as the chain's ``trigger_context``, which only
        the CORE default receives: USER handlers that run in between can observe or take over the call
        but not change whose it is.
        """
        caller = self.runtime.caller_for(ops)
        if caller is None or not caller.current():
            # Ops of a released activation start no chain; other packages never see a dead call.
            raise ValueError("package call belongs to an inactive activation")
        result = await self.runtime.bus.trigger(Event(event_name, make_input(caller.package_id)),
                                                ops.session_context, trigger_context=caller)
        if not isinstance(result, HostActionOutput):
            raise ValueError(f"{event_name} was not answered")
        if result.status == "rejected":
            # Unchanged public contract: invalid calls raise ValueError (S_08, S_09, S_10).
            raise ValueError(result.message or result.code or f"{event_name} rejected")
        answer: dict[str, Any] = {"status": result.status}
        if result.revision is not None:
            answer["revision"] = result.revision
        if result.code:
            answer["code"] = result.code
        return answer

    @staticmethod
    def _action_package(caller: Caller):
        """The application package of a host operation's caller, while its activation is current.

        Called on entry and again after each await that a release may overtake.
        """
        if not caller.application or not caller.current():
            raise ValueError("package call belongs to an inactive activation")
        return caller.package

    async def offer_download(self, caller: Caller, path: Any, filename: Any) -> dict[str, Any]:
        """Offer one data file to the calling connection (S_10 section 7.1)."""
        self._action_package(caller)
        return await self.transfers.offer_download(caller, path, filename)

    async def preview_splash(self, caller: Caller) -> dict[str, Any]:
        """Ask the calling connection to play this package's splash once (S_09 section 3.3).

        A splash the user turned off can still be previewed: the request comes from the package's own page.
        """
        package = self._action_package(caller)
        connection_id = caller.connection_id
        declaration = self.splash_declaration(package)
        if declaration is None:
            raise ValueError("ops.splash.preview requires an application.splash contribution")
        if not connection_id:
            raise ValueError("ops.splash.preview requires a browser connection")
        delivered = await self.runtime.webui.transport.send_splash_play(connection_id, {
            "package_id": package.package_id, "skippable": declaration["skippable"],
            "max_duration_ms": declaration["max_duration_ms"],
        })
        return {"status": "requested" if delivered else "delivery_failed"}

    def _package_caller(self, ops: EventHandlerOps):
        """Resolve the application package and caller of Ops from its current activation."""
        caller = self.runtime.caller_for(ops)
        if caller is None:
            raise ValueError("package call belongs to an inactive activation")
        return self._action_package(caller), caller

    def _client_caller(self, ops: EventHandlerOps):
        """The caller of a browser operation: a current client package and its open connection."""
        caller = self.runtime.caller_for(ops)
        if caller is None or not caller.current():
            raise ValueError("package call belongs to an inactive activation")
        connection = self.connections.get(caller.connection_id)
        if not caller.package.client or connection is None or connection.ws.closed:
            raise ValueError("inactive package or browser connection")
        return caller, connection

    async def operation(self, ops: EventHandlerOps, method: str, params: dict[str, Any]) -> dict:
        caller, connection = self._client_caller(ops)
        if method == "page.reload":
            if params:
                raise ValueError("page.reload does not accept parameters")
        elif method in {"local_storage.set", "local_storage.delete"}:
            self._storage_key(params.get("key"))
            if method == "local_storage.set":
                params = {"key": params["key"], "value": json_copy(params["value"], maximum=64 * 1024, depth=8)}
        else:
            raise ValueError("unsupported browser operation")
        metadata = ops.session_context.metadata
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("browser operations require a current request id")
        await self.runtime.channel.send_event(connection.ws, "client.operation", {
            "clientProtocolVersion": 1, "invocation_id": request_id,
            "connection_epoch": connection.epoch, "package_id": caller.package_id,
            "activation_generation": caller.generation, "method": method,
            "params": params, "deadline": int(time.time() * 1000) + 30_000,
        })
        return {"accepted": True, "status": "queued"}

    async def dispatch(self, request, context: SessionContext) -> UiActionOutput | None:
        params = request.params
        target = params.get("target")
        if not isinstance(target, Mapping):
            return None
        package_id = target.get("package_id", "")
        own = self.application_packages()
        if package_id and not any(package.package_id == package_id for package in own):
            return None
        slot_id = target.get("slot_id")
        if slot_id not in SLOTS:
            return None
        # application.settings is shared with AgentServer packages: only claim declared Gateway instances.
        if slot_id == SETTINGS_SLOT and (not package_id or not any(
                package.package_id == package_id and self._settings_instance(package, target.get("instance_key", ""))
                for package in own)):
            return None
        if (set(params) - {"event", "target", "payload"} or set(target) - {"scope", "slot_id", "package_id", "instance_key"}
                or target.get("scope") != "application"):
            return UiActionOutput(accepted=False, status="invalid_target", code="SCHEMA_ERROR")
        event_name = params.get("event")
        instance_key = target.get("instance_key", "")
        if not valid_instance_key(instance_key):
            return UiActionOutput(accepted=False, status="invalid_target", code="SCHEMA_ERROR")
        raw_payload = params.get("payload", {})
        if not isinstance(raw_payload, Mapping):
            return UiActionOutput(accepted=False, status="invalid_payload", code="SCHEMA_ERROR")
        payload = json_copy(dict(raw_payload), maximum=64 * 1024)
        if event_name == "ui.ready":
            accepted = False
            for package in own:
                if package_id and package_id != package.package_id:
                    continue
                if not any(item.get("slot") == slot_id for item in package.ui["items"]):
                    continue
                scoped = RenderTarget(session_id="", slot_id=slot_id, package_id=package.package_id,
                                      instance_key=instance_key, scope="application", connection_id=request.connection_id)
                result = await self.runtime.bus.trigger(Event(event_name, UiActionInput(
                    event_name, scoped, MappingProxyType(payload), request.request_context,
                )), context)
                accepted = accepted or (isinstance(result, UiActionOutput) and result.accepted)
            return UiActionOutput(accepted=accepted, status="ready" if accepted else "unhandled")
        if event_name not in UPSTREAM_EVENTS or not package_id:
            return UiActionOutput(accepted=False, status="invalid_event", code="SCHEMA_ERROR")
        scoped = RenderTarget(session_id="", slot_id=slot_id, package_id=package_id, instance_key=instance_key,
                              scope="application", connection_id=request.connection_id)
        record = self.runtime.registry.get(scoped)
        if record is None or record.root is None or payload.get("revision") != record.revision:
            return UiActionOutput(accepted=False, status="inactive", code="TARGET_INACTIVE")
        nodes = tree_nodes(record.root)
        if set(payload) != {"node_key", "revision", "action_id", "args", "data"}:
            return UiActionOutput(accepted=False, status="invalid_action", code="SCHEMA_ERROR")
        if not isinstance(payload.get("node_key"), str):
            return UiActionOutput(accepted=False, status="invalid_action", code="SCHEMA_ERROR")
        node = nodes.get(payload["node_key"])
        action = (node.events or {}).get(event_name) if node else None
        if (not action or payload["action_id"] != action["id"] or payload["args"] != action.get("args", {})
                or not self._valid_action(node, event_name, payload["data"], nodes)):
            return UiActionOutput(accepted=False, status="invalid_action", code="SCHEMA_ERROR")
        upload = None
        if node.component == "FileImport":
            # The browser only proves completion; the handler gets the host's own record (S_10 section 6.4).
            upload = self.transfers.claim(payload["data"]["upload_id"], scoped, payload["node_key"])
            if upload is None:
                return UiActionOutput(accepted=False, status="invalid_action", code="SCHEMA_ERROR")
            payload = {**payload, "data": upload.descriptor()}
        try:
            result = await self.runtime.bus.trigger(Event(event_name, UiActionInput(
                event_name, scoped, MappingProxyType(payload), request.request_context,
            )), context)
        finally:
            if upload is not None:
                self.transfers.discard_later(upload)
        return result if isinstance(result, UiActionOutput) else UiActionOutput(accepted=False, status="unhandled")

    @staticmethod
    def _valid_action(node: RenderNode, event: str, data: Any, nodes: dict[str, RenderNode]) -> bool:
        if not validate_event_data(node.component, event, data, node.props):
            return False
        if node.component != "Button" or "form" not in node.props:
            return True
        forms = [item for item in nodes.values() if item.component == "Form"
                 and item.props.get("name") == node.props["form"]]
        if len(forms) != 1:
            return False
        fields = {item.props["name"] for item in tree_nodes(forms[0]).values() if item.component == "Form.Item"}
        return set(data) == fields

    def asset_file(self, package_id: str, generation: str, relative: str) -> Path:
        """Resolve one readable asset of an active application package.

        ``<generation>`` addresses the package ``assets/`` directory and
        ``<generation>.data`` the package data ``public/`` directory.
        """
        data = generation.endswith(DATA_GENERATION_SUFFIX)
        if data:
            generation = generation[:-len(DATA_GENERATION_SUFFIX)]
        activation = self.runtime.current_activation(package_id)
        if activation is None or not activation.application or activation.generation != generation:
            raise ValueError("inactive package asset")
        owner = self.runtime.package_data_dir(package_id) if data else activation.package.root
        root = ((owner / "public") if data else (owner / "assets")).resolve()
        path = (root / relative).resolve()
        if not root.is_relative_to(owner.resolve()) or not path.is_relative_to(root) or path == root or not path.is_file():
            raise ValueError("asset path is outside the package")
        return path

    @staticmethod
    def _storage_key(key: Any) -> None:
        if not isinstance(key, str) or not key.strip() or len(key.encode("utf-8")) > 128:
            raise ValueError("invalid localStorage key")

    @staticmethod
    def _path(parts: Any) -> None:
        if (not isinstance(parts, list) or len(parts) > 8
                or any(not isinstance(part, str) or not part or part in FORBIDDEN for part in parts)):
            raise ValueError("invalid binding path")
