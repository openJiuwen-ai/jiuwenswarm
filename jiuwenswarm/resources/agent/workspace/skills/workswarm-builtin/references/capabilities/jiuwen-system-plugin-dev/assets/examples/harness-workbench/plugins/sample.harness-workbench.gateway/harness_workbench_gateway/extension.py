# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply the workbench choice to the first chat.send of a new session."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace

from agent_gateway.extensions_v2.models import (
    GatewayRequestInput,
    GatewayRequestOutput,
    GatewayRoute,
)
from agent_gateway.extensions_v2.webui.models import UiActionInput

from .choice import MODES, HarnessNewTaskChoice, harness_mode
from .theme import BACKGROUND_SLOT, apply_theme

logger = logging.getLogger(__name__)
_SESSION_OPEN_METHODS = frozenset({"session.get_metadata", "history.get"})
_SESSION_MODE_PREFIX = "harness-session-mode:"


class HarnessWorkbenchGatewayExtension:
    """Watch workbench selections and new sessions on each browser connection."""

    def __init__(self) -> None:
        self._choice = HarnessNewTaskChoice()
        self._viewed: dict[str, str] = {}
        self._shown: dict[str, str] = {}
        self._pinned: set[str] = set()

    def register(self, registrar) -> None:
        """Subscribe to inbound frames, the background slot, and connection close."""
        registrar.register_event_handler("gateway.request", self.on_request)
        registrar.register_event_handler("gateway.disconnect", self.on_disconnect)
        registrar.register_event_handler(
            "ui.ready",
            self.on_background_ready,
            {"target.slot_id": BACKGROUND_SLOT},
            "user",
        )

    async def on_request(self, event, ops, next_handler):
        """Forward the frame, replacing params only for an armed first send."""
        receipt = await next_handler(event)
        request = event.event_input
        if not isinstance(request, GatewayRequestInput):
            return receipt
        if request.method == "webui.dispatch":
            mode = self._choice.remember_selection(request.params, request.connection_id)
            if mode is not None and ops is not None and request.connection_id:
                self._pinned.add(request.connection_id)
                await self._apply_theme(ops, mode, request.connection_id)
            return receipt
        route = GatewayRoute.FORWARD
        source = request.params
        if isinstance(receipt, GatewayRequestOutput):
            parsed = GatewayRoute.parse(receipt.route)
            if parsed is not None:
                route = parsed
            if isinstance(receipt.params, Mapping):
                source = receipt.params
        if route is not GatewayRoute.FORWARD:
            return receipt
        session_id = _session_id(source)
        previous = self._viewed.get(request.connection_id, "")
        if session_id and request.connection_id:
            self._viewed[request.connection_id] = session_id
        rewritten = self._choice.observe(request.method, source, request.connection_id)
        if (
            ops is not None
            and request.connection_id
            and request.method in (*_SESSION_OPEN_METHODS, "session.create")
        ):
            same_view = bool(session_id) and session_id == previous and request.connection_id in self._pinned
            if not same_view:
                self._pinned.discard(request.connection_id)
                await self._sync_theme(ops, request.connection_id, session_id)
        effective = rewritten if rewritten is not None else source
        if request.method == "chat.send" and ops is not None:
            mode = harness_mode(effective)
            if mode is None and rewritten is not None:
                mode = self._choice.selected_mode(request.connection_id)
            if mode is not None:
                self._pinned.discard(request.connection_id)
                if session_id:
                    await self._save_session_mode(ops, session_id, mode)
                await self._apply_theme(ops, mode, request.connection_id)
        if rewritten is None:
            return receipt
        if isinstance(receipt, GatewayRequestOutput):
            return replace(receipt, params=rewritten)
        return GatewayRequestOutput(route=GatewayRoute.FORWARD.value, params=rewritten)

    async def on_background_ready(self, event, ops, next_handler):
        """Reapply the theme already chosen for this connection."""
        receipt = await next_handler(event)
        request = event.event_input
        if not isinstance(request, UiActionInput) or ops is None:
            return receipt
        connection_id = request.target.connection_id.strip()
        if connection_id:
            shown = self._shown.get(connection_id)
            if shown is None:
                await self._sync_theme(ops, connection_id)
            else:
                await self._apply_theme(ops, shown, connection_id)
        return receipt

    async def on_disconnect(self, event, ops, next_handler):
        """Drop the closed browser connection."""
        del ops
        connection_id = getattr(event.event_input, "connection_id", "")
        self._choice.drop(connection_id)
        self._viewed.pop(connection_id, None)
        self._shown.pop(connection_id, None)
        self._pinned.discard(connection_id)
        return await next_handler(event)

    async def _theme_for_view(self, ops, connection_id: str, session_id: str = "") -> str:
        """Bound sessions keep their theme; existing native chats restore the host look."""
        session_id = session_id or self._viewed.get(connection_id, "")
        if not session_id:
            return self._choice.selected_mode(connection_id)
        saved = await self._load_session_mode(ops, session_id)
        if saved:
            return saved
        if self._choice.is_new_task_session(connection_id, session_id):
            return self._choice.selected_mode(connection_id)
        return "normal"

    async def _sync_theme(self, ops, connection_id: str, session_id: str = "") -> None:
        """Paint the theme that belongs to the session currently on screen."""
        await self._apply_theme(ops, await self._theme_for_view(ops, connection_id, session_id), connection_id)

    async def _apply_theme(self, ops, mode: str, connection_id: str) -> None:
        """Keep visual decoration optional so routing never depends on theme delivery."""
        shown = mode if mode in MODES else "normal"
        if connection_id:
            self._shown[connection_id] = shown
        try:
            await apply_theme(ops, shown, connection_id)
        except Exception:
            logger.warning("[harness-workbench] theme preview failed for %s", shown, exc_info=True)

    @staticmethod
    async def _save_session_mode(ops, session_id: str, mode: str) -> None:
        """Persist the trusted mode so reopening a session restores its theme."""
        store = getattr(ops, "store", None)
        if store is None:
            return
        try:
            await store.set(f"{_SESSION_MODE_PREFIX}{session_id}", mode)
        except Exception:
            logger.warning("[harness-workbench] cannot persist the session theme", exc_info=True)

    @staticmethod
    async def _load_session_mode(ops, session_id: str) -> str | None:
        """Read a previously observed session mode from package storage."""
        store = getattr(ops, "store", None)
        if store is None:
            return None
        try:
            mode = await store.get(f"{_SESSION_MODE_PREFIX}{session_id}")
        except Exception:
            logger.warning("[harness-workbench] cannot restore the session theme", exc_info=True)
            return None
        return mode if isinstance(mode, str) and mode in MODES else None


def create_extension() -> HarnessWorkbenchGatewayExtension:
    """Instantiated when the Gateway package is activated."""
    return HarnessWorkbenchGatewayExtension()


def _session_id(params: object) -> str:
    """Read the normalized session id from forwarded request parameters."""
    if not isinstance(params, Mapping):
        return ""
    value = params.get("session_id")
    return value.strip() if isinstance(value, str) else ""
