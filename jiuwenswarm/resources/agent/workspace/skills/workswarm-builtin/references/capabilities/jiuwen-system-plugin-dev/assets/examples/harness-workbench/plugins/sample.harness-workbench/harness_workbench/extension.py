# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render the next-task selector and the chrome of a bound Harness session."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.context_usage import CONTEXT_USAGE_EVENT, ContextUsageUpdate
from jiuwenswarm.extensions_v2.event_types.session_history import (
    SESSION_APPEND_EVENT,
    SessionAppendInput,
    SessionAppendResult,
)
from jiuwenswarm.extensions_v2.webui.call_context import current_connection
from jiuwenswarm.extensions_v2.webui.models import (
    RenderMode,
    RenderStatus,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
)
from jiuwenswarm.extensions_v2.webui.resources import complete_before_cancel

from .activity import build_snapshot, panel_has_content
from .panel import write_claude_panel
from .binding import bound_mode, load_metadata
from .experts import HarnessExpertConflict, ensure_harness_experts
from .transcript import merge_observed_tools, read_tool_records
from .views import (
    ACTION_MODE,
    ACTIVITY_SLOT,
    CHOICES,
    CLAUDE_CTX_METER_ASSET,
    CLAUDE_CTX_METER_FALLBACK,
    CLAUDE_MARK_ASSET,
    CLAUDE_TITLE_MARK_ASSET,
    CLAUDE_TITLE_FALLBACK_ASSET,
    CLAUDE_PULSE_ASSET,
    CLAUDE_PULSE_FALLBACK,
    CODEX_LIVE_ASSET,
    CODEX_LIVE_FALLBACK,
    CODEX_MARK_ASSET,
    CODEX_MARK_FALLBACK,
    NORMAL_CHAT_ASSET,
    COMPOSER_INSTANCE,
    COMPOSER_SLOT,
    FOOTER_INSTANCE,
    FOOTER_SLOT,
    PACKAGE_ID,
    PANEL_INSTANCE,
    PANEL_SLOT,
    STATUS_INSTANCE,
    STATUS_SLOT,
    TITLE_INSTANCE,
    TITLE_SLOT,
    activity_view,
    composer_view,
    footer_choice_view,
    panel_view,
    title_view,
)

logger = logging.getLogger(__name__)
_COMPOSER_CHOICE_FILTER = {"target.slot_id": COMPOSER_SLOT}
_FOOTER_CHOICE_FILTER = {"target.slot_id": FOOTER_SLOT}
_SESSION_SLOTS = (STATUS_SLOT, TITLE_SLOT, ACTIVITY_SLOT, PANEL_SLOT)
_FIXED_INSTANCES = {
    STATUS_SLOT: STATUS_INSTANCE,
    TITLE_SLOT: TITLE_INSTANCE,
    PANEL_SLOT: PANEL_INSTANCE,
}
_MAX_OBSERVED_RECORDS = 64


@dataclass
class _SessionChrome:
    """Which native slots have mounted for one chat."""

    status: bool = False
    title: bool = False
    panel: bool = False
    latest_activity: str = ""
    panel_resource_id: str = ""


class HarnessWorkbenchExtension:
    """Install the fixed experts, remember the next task, and paint a bound session."""

    def __init__(self) -> None:
        self._experts_ready = False
        self._experts_lock = asyncio.Lock()
        self._modes: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        self._viewed: dict[str, str] = {}
        self._viewed_bound: dict[str, str] = {}
        self._viewed_existing: dict[str, bool] = {}
        self._previewing: set[str] = set()
        self._sessions: dict[str, _SessionChrome] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._records: dict[str, list[dict]] = {}
        self._claude_mark = ""
        self._claude_title_mark = ""
        self._codex_mark = ""
        self._chrome_urls: dict[str, str] = {}
        self._asset_cache_token: tuple[str, str, str, str] = ("", "", "", "")

    def register(self, registrar) -> None:
        """Register handlers; bootstrap experts when the selector first becomes ready."""
        registrar.register_event_handler("ui.ready", self.on_footer_ready, {"target.slot_id": FOOTER_SLOT}, "user")
        for event in ("ui.select", "ui.click"):
            registrar.register_event_handler(event, self.on_select, _FOOTER_CHOICE_FILTER, "user")
            registrar.register_event_handler(event, self.on_select, _COMPOSER_CHOICE_FILTER, "user")
        for slot in _SESSION_SLOTS:
            registrar.register_event_handler("ui.ready", self.on_session, {"target.slot_id": slot}, "user")
        registrar.register_event_handler(CONTEXT_USAGE_EVENT, self.on_usage, {}, "user")
        for door in ("tool-call", "response"):
            registrar.register_event_handler(SESSION_APPEND_EVENT, self.on_history, {"door": door}, "user")

    async def _ensure_experts(self) -> None:
        """Await the catalog in a business handler, never in synchronous registration."""
        async with self._experts_lock:
            if self._experts_ready:
                return
            try:
                await ensure_harness_experts()
            except HarnessExpertConflict:
                logger.error("[harness-workbench] fixed experts conflict; existing experts are unchanged")
            except Exception:
                logger.warning("[harness-workbench] expert bootstrap failed", exc_info=True)
            else:
                self._experts_ready = True

    async def on_footer_ready(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        """Paint the connection-level selector when there is no chat session yet."""
        receipt = await next_handler(event)
        if not getattr(receipt, "accepted", True) or ops is None:
            return receipt
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return receipt
        target = request.target
        if target.slot_id != FOOTER_SLOT or target.package_id not in ("", PACKAGE_ID):
            return receipt
        await self._ensure_experts()
        connection_id = _connection_id(request, ops)
        if not connection_id:
            logger.warning("[harness-workbench] footer ready without connection_id; skipping rail render")
        elif not await self._render_footer(ops, connection_id):
            logger.warning("[harness-workbench] footer render was not delivered")
        return receipt

    async def on_select(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        """Remember the next task and mirror it on native chats. Bound sessions stay put."""
        receipt = await next_handler(event)
        if not getattr(receipt, "accepted", True) or ops is None:
            return receipt
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.payload.get("action_id") != ACTION_MODE:
            return receipt
        connection_id = _connection_id(request, ops)
        value = _clicked_mode(request.payload)
        if not connection_id or value not in CHOICES:
            return receipt
        self._modes[connection_id] = value
        self._previewing.add(connection_id)
        if not await self._render_footer(ops, connection_id):
            return UiActionOutput(False, "render_failed")
        if not await self._mirror_pending(ops, connection_id, value):
            return UiActionOutput(False, "render_failed")
        return receipt

    async def on_session(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        """Paint chrome from the stored binding, and clear it for ordinary chat."""
        receipt = await next_handler(event)
        if not getattr(receipt, "accepted", True) or ops is None:
            return receipt
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.package_id not in ("", PACKAGE_ID):
            return receipt
        session_id = request.target.session_id.strip()
        if not session_id:
            return receipt
        connection_id = _connection_id(request, ops)
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            state = self._sessions.setdefault(session_id, _SessionChrome())
            if not _owns(request.target.slot_id, request.target.instance_key):
                return receipt
            _remember(state, request.target.slot_id, request.target.instance_key)
            if request.target.slot_id == TITLE_SLOT:
                mode = _bound_mode(ops, session_id)
                if mode == "claude":
                    mark = await self._claude_title_mark_src(ops)
                    fallback = await self._chrome_src(ops, CLAUDE_TITLE_FALLBACK_ASSET)
                    await self._render_fixed(
                        ops,
                        session_id,
                        TITLE_SLOT,
                        TITLE_INSTANCE,
                        title_view(
                            "claude",
                            claude_mark=mark,
                            claude_title_fallback=fallback,
                        ),
                    )
            if not await self._show(ops, session_id, state, connection_id):
                if state.status and await self._render_fixed(
                    ops,
                    session_id,
                    COMPOSER_SLOT,
                    COMPOSER_INSTANCE,
                    composer_view("normal"),
                ):
                    logger.warning("[harness-workbench] session chrome degraded: %s", session_id)
                else:
                    return UiActionOutput(False, "render_failed")
            return receipt

    async def on_usage(self, event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> object:
        """Refresh bound chrome after a round. The next-task selector stays unchanged."""
        receipt = await next_handler(event)
        update = event.event_input
        if (not isinstance(update, ContextUsageUpdate) or ops is None or ops.session_context is None
                or ops.session_context.session_id != update.session_id):
            return receipt
        session_id = update.session_id
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            state = self._sessions.get(session_id)
            if state is None:
                return receipt
            if not await self._show(ops, session_id, state):
                logger.warning("[harness-workbench] chrome refresh failed: %s", session_id)
        return receipt

    async def on_history(self, event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> object:
        """Refresh inline activity after the Harness transcript row has been committed."""
        receipt = await next_handler(event)
        update = event.event_input
        if (
            not isinstance(update, SessionAppendInput)
            or not isinstance(receipt, SessionAppendResult)
            or receipt.status not in ("persisted", "duplicate")
            or ops is None
            or ops.session_context is None
            or ops.session_context.session_id != receipt.session_id
        ):
            return receipt
        session_id = receipt.session_id
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            self._remember_record(session_id, receipt.history_record)
            state = self._sessions.get(session_id)
            if state is None or not state.latest_activity:
                return receipt
            if not await self._show(ops, session_id, state):
                logger.warning("[harness-workbench] transcript refresh failed: %s", session_id)
        return receipt

    async def _show(
        self,
        ops: EventHandlerOps,
        session_id: str,
        state: _SessionChrome,
        connection_id: str = "",
    ) -> bool:
        """Paint the status, then either the harness chrome or a clear of the extra slots."""
        mode, messages = await self._resolve(ops, session_id)
        if connection_id:
            spoken = isinstance(messages, list) and any(isinstance(item, Mapping) for item in messages)
            if self._viewed.get(connection_id) != session_id:
                self._previewing.discard(connection_id)
            self._viewed[connection_id] = session_id
            self._viewed_bound[connection_id] = mode if mode in CHOICES else "normal"
            self._viewed_existing[connection_id] = mode == "normal" and spoken
            if not await self._render_footer(ops, connection_id):
                logger.warning("[harness-workbench] footer icon did not follow session %s", session_id)
        shown, pending = self._status_choice(session_id, mode, connection_id)
        bound = mode != "normal"
        chrome = await self._composer_chrome(
            ops,
            claude=shown == "claude" or mode == "claude",
            codex=shown == "codex" or mode == "codex",
        )
        ok = True
        if state.status:
            ok = await self._render_fixed(
                ops,
                session_id,
                COMPOSER_SLOT,
                COMPOSER_INSTANCE,
                composer_view(
                    shown,
                    bound=bound,
                    bound_mode=mode,
                    **chrome,
                ),
            )
        if mode == "normal":
            return await self._clear_chrome(ops, session_id, state) and ok
        painted = await self._paint(ops, session_id, state, mode, messages)
        if mode == "claude":
            try:
                await self._paint_claude_extras(ops, session_id, state, messages)
            except Exception:
                logger.warning("[harness-workbench] claude extras failed: %s", session_id, exc_info=True)
        return painted and ok

    def _status_choice(self, session_id: str, mode: str, connection_id: str) -> tuple[str, bool]:
        """Use the binding once it exists. A native chat shows the next-task choice."""
        if mode != "normal":
            self._pending.pop(session_id, None)
            return mode, False
        chosen = self._modes.get(connection_id, "") if connection_id else ""
        if chosen:
            self._pending[session_id] = chosen
        else:
            chosen = self._pending.get(session_id, "normal")
        if chosen not in CHOICES:
            chosen = "normal"
        return chosen, chosen != "normal"

    async def _mirror_pending(self, ops: EventHandlerOps, connection_id: str, value: str) -> bool:
        """Update native composer rows. A session already bound to a Harness is left alone."""
        ok = True
        for session_id, state in list(self._sessions.items()):
            if not state.status:
                continue
            async with self._locks.setdefault(session_id, asyncio.Lock()):
                if _bound_mode(ops, session_id) != "normal":
                    continue
                self._pending[session_id] = value
                chrome = await self._composer_chrome(
                    ops,
                    claude=value == "claude",
                    codex=value == "codex",
                )
                rendered = await self._render_fixed(
                    ops,
                    session_id,
                    COMPOSER_SLOT,
                    COMPOSER_INSTANCE,
                    composer_view(value, bound=False, **chrome),
                )
                ok = rendered and ok
        return ok

    async def _resolve(self, ops: EventHandlerOps, session_id: str) -> tuple[str, object]:
        """Read the trusted binding and messages. A read failure falls back to ordinary chat."""
        metadata = _binding_metadata(ops, session_id)
        mode = "normal" if metadata is None else (bound_mode(metadata) or "normal")
        try:
            messages = await ops.session.messages()
        except Exception:
            logger.warning("[harness-workbench] messages unreadable: %s", session_id, exc_info=True)
            messages = None
        records = await asyncio.to_thread(read_tool_records, session_id)
        observed = _merge_records(records, self._records.get(session_id, []))
        return mode, merge_observed_tools(messages, observed)

    async def _harness_marks(
        self,
        ops: EventHandlerOps,
        *,
        claude: bool,
        codex: bool,
    ) -> tuple[str, str]:
        """Resolve packaged marks for composer chrome."""
        claude_mark = await self._claude_mark_src(ops) if claude else ""
        codex_mark = await self._codex_mark_src(ops) if codex else ""
        return claude_mark, codex_mark

    def _sync_asset_cache(self, ops: EventHandlerOps) -> None:
        """Drop cached asset URLs after a package reinstall, activation or connection change."""
        metadata = getattr(ops, "package_metadata", None)
        connection = current_connection()
        token = (
            getattr(metadata, "package_id", "") or "",
            getattr(metadata, "package_version", "") or "",
            str(getattr(metadata, "root", "") or ""),
            connection.connection_id if connection is not None else "",
        )
        if token == self._asset_cache_token:
            return
        self._asset_cache_token = token
        self._claude_mark = ""
        self._claude_title_mark = ""
        self._codex_mark = ""
        self._chrome_urls.clear()

    async def _chrome_src(self, ops: EventHandlerOps, filename: str) -> str:
        """Resolve one packaged chrome asset URL and cache it for this activation."""
        self._sync_asset_cache(ops)
        cached = self._chrome_urls.get(filename)
        if cached is not None:
            return cached
        url = await self._asset_src(ops, filename, filename)
        self._chrome_urls[filename] = url
        return url

    async def _composer_chrome(
        self,
        ops: EventHandlerOps,
        *,
        claude: bool,
        codex: bool,
    ) -> dict[str, str]:
        """URLs for composer-row marks and animated chrome."""
        claude_mark, codex_mark = await self._harness_marks(ops, claude=claude, codex=codex)
        chrome = {"claude_mark": claude_mark, "codex_mark": codex_mark}
        if claude:
            chrome["claude_pulse"] = await self._chrome_src(ops, CLAUDE_PULSE_ASSET)
            chrome["claude_pulse_fallback"] = await self._chrome_src(ops, CLAUDE_PULSE_FALLBACK)
            chrome["claude_meter"] = await self._chrome_src(ops, CLAUDE_CTX_METER_ASSET)
            chrome["claude_meter_fallback"] = await self._chrome_src(ops, CLAUDE_CTX_METER_FALLBACK)
        if codex:
            chrome["codex_live"] = await self._chrome_src(ops, CODEX_LIVE_ASSET)
            chrome["codex_live_fallback"] = await self._chrome_src(ops, CODEX_LIVE_FALLBACK)
            chrome["codex_mark_fallback"] = await self._chrome_src(ops, CODEX_MARK_FALLBACK)
        return chrome

    async def _claude_mark_src(self, ops: EventHandlerOps) -> str:
        """Resolve the packaged Claude avatar for composer chrome."""
        self._sync_asset_cache(ops)
        if self._claude_mark:
            return self._claude_mark
        self._claude_mark = await self._asset_src(ops, CLAUDE_MARK_ASSET, "claude mark")
        return self._claude_mark

    async def _claude_title_mark_src(self, ops: EventHandlerOps) -> str:
        """Resolve the orange Claude SVG for the conversation title row."""
        self._sync_asset_cache(ops)
        if self._claude_title_mark:
            return self._claude_title_mark
        self._claude_title_mark = await self._asset_src(ops, CLAUDE_TITLE_MARK_ASSET, "claude title mark")
        return self._claude_title_mark

    async def _codex_mark_src(self, ops: EventHandlerOps) -> str:
        """Resolve the packaged Codex mark once. Missing assets keep the file-code icon."""
        self._sync_asset_cache(ops)
        if self._codex_mark:
            return self._codex_mark
        self._codex_mark = await self._asset_src(ops, CODEX_MARK_ASSET, "codex mark")
        if not self._codex_mark:
            self._codex_mark = await self._asset_src(ops, CODEX_MARK_FALLBACK, "codex mark fallback")
        return self._codex_mark

    async def _footer_trigger_src(self, ops: EventHandlerOps, mode: str) -> str:
        """Brand mark for the sidebar footer: the open session, else the next task."""
        if mode == "claude":
            mark = await self._claude_mark_src(ops)
            return mark or await self._asset_src(ops, CLAUDE_TITLE_FALLBACK_ASSET, "claude mark")
        if mode == "codex":
            return await self._codex_mark_src(ops)
        return await self._asset_src(ops, NORMAL_CHAT_ASSET, "normal chat mark")

    async def _asset_src(self, ops: EventHandlerOps, filename: str, label: str) -> str:
        """Load one package asset URL; failures return empty and fall back in the view tree."""
        asset_url = getattr(getattr(ops, "ui", None), "asset_url", None)
        if asset_url is None:
            return ""
        try:
            src = await asset_url(filename)
        except Exception:
            logger.warning("[harness-workbench] %s unavailable", label, exc_info=True)
            return ""
        return src if isinstance(src, str) and src else ""

    def _remember_record(self, session_id: str, record: object) -> None:
        """Keep recent committed tool rows until the transcript reader sees them."""
        if not isinstance(record, Mapping) or record.get("event_type") != "chat.tool_call":
            return
        records = self._records.setdefault(session_id, [])
        key = _record_key(record)
        if key and any(_record_key(item) == key for item in records):
            return
        records.append(copy.deepcopy(dict(record)))
        if len(records) > _MAX_OBSERVED_RECORDS:
            del records[:-_MAX_OBSERVED_RECORDS]

    async def _paint(
        self,
        ops: EventHandlerOps,
        session_id: str,
        state: _SessionChrome,
        mode: str,
        messages: object,
    ) -> bool:
        """Refresh every harness slot that has already mounted for this chat."""
        ok = True
        if state.title:
            _, codex_mark = await self._harness_marks(
                ops,
                claude=False,
                codex=mode == "codex",
            )
            claude_title = await self._claude_title_mark_src(ops) if mode == "claude" else ""
            claude_title_fallback = (
                await self._chrome_src(ops, CLAUDE_TITLE_FALLBACK_ASSET) if mode == "claude" else ""
            )
            codex_mark_fallback = await self._chrome_src(ops, CODEX_MARK_FALLBACK) if mode == "codex" else ""
            ok = await self._render_fixed(
                ops,
                session_id,
                TITLE_SLOT,
                TITLE_INSTANCE,
                title_view(
                    mode,
                    claude_mark=claude_title,
                    claude_title_fallback=claude_title_fallback,
                    codex_mark=codex_mark,
                    codex_mark_fallback=codex_mark_fallback,
                ),
            ) and ok
        if state.latest_activity:
            snapshot = await self._snapshot(ops, session_id, mode, messages)
            output = await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id, ACTIVITY_SLOT, PACKAGE_ID, state.latest_activity),
                root=activity_view(snapshot),
            ))
            ok = _delivered(output) and ok
        return ok

    async def _clear_chrome(self, ops: EventHandlerOps, session_id: str, state: _SessionChrome) -> bool:
        """Remove the Harness-only title and inline activity from an ordinary chat."""
        ok = True
        if state.title:
            ok = await self._clear(ops, session_id, TITLE_SLOT, TITLE_INSTANCE) and ok
        if state.latest_activity:
            ok = await self._clear(ops, session_id, ACTIVITY_SLOT, state.latest_activity) and ok
        if state.panel:
            ok = await self._clear(ops, session_id, PANEL_SLOT, PANEL_INSTANCE) and ok
        if state.panel_resource_id:
            try:
                await ops.ui.release_resources(state.panel_resource_id)
            except Exception:
                logger.warning("[harness-workbench] panel resource cleanup failed: %s", session_id, exc_info=True)
            state.panel_resource_id = ""
        return ok

    async def _paint_claude_extras(
        self,
        ops: EventHandlerOps,
        session_id: str,
        state: _SessionChrome,
        messages: object,
    ) -> None:
        """Render Claude-only slots; failures must not remove the harness switch."""
        if not state.panel:
            return
        snapshot = await self._snapshot(ops, session_id, "claude", messages)
        if not panel_has_content(snapshot):
            await self._clear(ops, session_id, PANEL_SLOT, PANEL_INSTANCE)
            return
        if not await self._render_claude_panel(ops, session_id, state, messages, snapshot):
            logger.warning("[harness-workbench] right panel not delivered: %s", session_id)

    async def _render_claude_panel(
        self,
        ops: EventHandlerOps,
        session_id: str,
        state: _SessionChrome,
        messages: object,
        snapshot=None,
    ) -> bool:
        """Publish a fresh panel document, then mount the right-hand iframe."""
        create_dir = getattr(getattr(ops, "ui", None), "create_resource_dir", None)
        if create_dir is None:
            logger.warning("[harness-workbench] create_resource_dir unavailable; skip panel")
            return False
        previous = state.panel_resource_id.strip()
        resources = await create_dir(previous=previous)
        try:
            if snapshot is None:
                snapshot = await self._snapshot(ops, session_id, "claude", messages)
            async with resources:
                await complete_before_cancel(asyncio.to_thread(write_claude_panel, resources.path, snapshot))
            sources = {
                "zh": await ops.ui.asset_url("zh.html", resource_id=resources.resource_id),
                "en": await ops.ui.asset_url("en.html", resource_id=resources.resource_id),
            }
            output = await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id, PANEL_SLOT, PACKAGE_ID, PANEL_INSTANCE),
                root=panel_view(sources),
            ))
        except BaseException as error:
            try:
                await ops.ui.release_resources(resources.resource_id)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                if isinstance(error, asyncio.CancelledError):
                    raise error from BaseExceptionGroup(
                        "WebUI panel write and cleanup failed", [error.__cause__, cleanup_error],
                    )
                raise
            raise
        delivered = _delivered(output)
        if delivered:
            state.panel_resource_id = resources.resource_id
            if previous and previous != resources.resource_id:
                try:
                    await ops.ui.release_resources(previous)
                except Exception:
                    logger.warning("[harness-workbench] stale panel resource: %s", session_id, exc_info=True)
        else:
            try:
                await ops.ui.release_resources(resources.resource_id)
            except Exception:
                logger.warning("[harness-workbench] panel render rollback failed: %s", session_id, exc_info=True)
        return delivered

    async def _snapshot(
        self,
        ops: EventHandlerOps,
        session_id: str,
        mode: str,
        messages: object,
    ):
        files: object = {}
        try:
            files = await ops.session.files()
        except Exception:
            logger.warning("[harness-workbench] files unreadable: %s", session_id, exc_info=True)
        return build_snapshot(mode, messages if isinstance(messages, list) else [], files)

    async def _render_fixed(self, ops: EventHandlerOps, session_id: str, slot: str, instance: str, root) -> bool:
        output = await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id, slot, PACKAGE_ID, instance),
            root=root,
        ))
        return _delivered(output)

    async def _clear(self, ops: EventHandlerOps, session_id: str, slot: str, instance: str) -> bool:
        output = await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id, slot, PACKAGE_ID, instance),
            mode=RenderMode.CLEAR,
        ))
        return _delivered(output)

    def _footer_mode(self, connection_id: str) -> str:
        """A click shows that choice. Opening another session shows that session."""
        pending = self._modes.get(connection_id, "normal")
        if connection_id in self._previewing and pending in CHOICES:
            return pending
        bound = self._viewed_bound.get(connection_id, "normal")
        if bound in CHOICES and bound != "normal":
            return bound
        if self._viewed_existing.get(connection_id):
            return "normal"
        return pending if pending in CHOICES else "normal"

    async def _render_footer(self, ops: EventHandlerOps, connection_id: str) -> bool:
        """Paint the icon-rail footer selector for one browser connection."""
        mode = self._footer_mode(connection_id)
        trigger_src = await self._footer_trigger_src(ops, mode)
        output = await ops.ui.render(UiRenderInput(
            target=RenderTarget("", FOOTER_SLOT, PACKAGE_ID, FOOTER_INSTANCE, "application", connection_id),
            root=footer_choice_view(mode, trigger_src=trigger_src),
        ))
        return _delivered(output)


def _remember(state: _SessionChrome, slot: str, instance_key: str) -> None:
    if slot == STATUS_SLOT:
        state.status = True
    elif slot == TITLE_SLOT:
        state.title = True
    elif slot == PANEL_SLOT:
        state.panel = True
    elif slot == ACTIVITY_SLOT:
        state.latest_activity = instance_key


def _owns(slot: str, instance_key: str) -> bool:
    expected = _FIXED_INSTANCES.get(slot)
    if expected is not None:
        return instance_key in ("", expected)
    return bool(instance_key.strip())


def _clicked_mode(payload: Mapping) -> str:
    args = payload.get("args")
    if isinstance(args, Mapping) and args.get("value") in CHOICES:
        return str(args["value"])
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    value = data.get("value")
    if value not in CHOICES:
        value = data.get("key")
    if value in CHOICES:
        return str(value)
    return ""


def _delivered(output: object) -> bool:
    status = getattr(output, "status", None)
    return status == RenderStatus.DELIVERED or status == "delivered"


def _bound_mode(ops: EventHandlerOps, session_id: str) -> str:
    """Return the trusted mode for one session, or normal when it is unbound."""
    context = getattr(ops, "session_context", None)
    context_session = str(getattr(context, "session_id", "") or "") if context is not None else ""
    if context is not None and context_session == session_id:
        metadata = getattr(context, "metadata", None)
        if isinstance(metadata, Mapping) and "session_runtime_binding" in metadata:
            return bound_mode(metadata) or "normal"
    metadata = load_metadata(session_id)
    if not isinstance(metadata, Mapping):
        return "normal"
    return bound_mode(metadata) or "normal"


def _binding_metadata(ops: EventHandlerOps, session_id: str) -> Mapping[str, object] | None:
    """Prefer a binding already on the event, then the persisted session metadata."""
    context = getattr(ops, "session_context", None)
    metadata = getattr(context, "metadata", None) if context is not None else None
    if isinstance(metadata, Mapping) and "session_runtime_binding" in metadata:
        return metadata
    return load_metadata(session_id)


def _merge_records(persisted: object, observed: object) -> list[dict]:
    """Append receipt rows missing from the current persisted transcript snapshot."""
    merged: list[dict] = []
    keys: set[str] = set()
    for source in (persisted, observed):
        if not isinstance(source, list):
            continue
        for record in source:
            if not isinstance(record, Mapping):
                continue
            key = _record_key(record)
            if key and key in keys:
                continue
            merged.append(dict(record))
            if key:
                keys.add(key)
    return merged


def _record_key(record: Mapping) -> str:
    """Return the stable row identity supplied by the history commit receipt."""
    for name in ("uuid", "id"):
        value = record.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _connection_id(request: UiActionInput, ops: EventHandlerOps | None) -> str:
    """Use the browser connection that owns this conversation."""
    connection_id = request.target.connection_id.strip()
    if connection_id:
        return connection_id
    found = _ops_connection(ops)
    if found:
        return found
    ctx = current_connection()
    if ctx is not None and ctx.connection_id.strip():
        return ctx.connection_id.strip()
    return ""


def _ops_connection(ops: EventHandlerOps | None) -> str:
    connection = current_connection()
    if connection is not None and connection.connection_id.strip():
        return connection.connection_id.strip()
    context = getattr(ops, "session_context", None) if ops is not None else None
    metadata = getattr(context, "metadata", None) if context is not None else None
    if isinstance(metadata, Mapping):
        for name in ("connection_id", "ws_id"):
            value = metadata.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def create_extension() -> HarnessWorkbenchExtension:
    """Instantiated when the package is activated."""
    return HarnessWorkbenchExtension()
