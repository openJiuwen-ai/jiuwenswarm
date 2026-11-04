# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Handle ready and component events; send new trees through ops.ui.render."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
)
from jiuwenswarm.extensions_v2.webui.models import (
    RenderMode,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

from .state import SessionState
from .views import (
    IFRAME_INSTANCE,
    IFRAME_PAGE_SLOTS,
    INSTANCE,
    PACKAGE_ID,
    QUICK_LAUNCH_ACTION,
    SLOTS,
    TASK_INSTANCE,
    TURN_SLOTS,
    TURN_SELECT_ACTION,
    USER_INSTANCE,
    USER_LOGIN_ACTION,
    USER_SELECT_ACTION,
    WEATHER_HIDE_ACTION,
    WEATHER_SHOW_ACTION,
    build_view,
    signed_out_view,
    user_menu_view,
    welcome_page,
)
from .workspace import apply_file_action, load_workspace

EVENTS = ("ui.click", "ui.change", "ui.press_enter", "ui.search", "ui.clear", "ui.select")


class WorkbenchHandlers:
    """Keep session and round state inside one extension activation."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._signed_in: dict[str, bool] = {}

    def handlers(self) -> tuple:
        return ((CONTEXT_USAGE_EVENT, self.on_usage, {}),) + tuple(
            (event, self.on_event, {"target.slot_id": slot})
            for slot in SLOTS for event in ("ui.ready", *EVENTS)
        )

    async def on_usage(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> ContextUsageOutput:
        update = event.event_input
        if (not isinstance(update, ContextUsageUpdate) or ops is None
                or ops.session_context is None or ops.session_context.session_id != update.session_id
                or not update.request_id):
            return await next_handler(event)
        lock = self._locks.setdefault(update.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.setdefault(update.session_id, SessionState())
            if await self._load_usage(ops, update.session_id, update.request_id, state):
                slot = "conversation.session.content.after"
                if (slot, update.request_id) in state.mounted:
                    await self._render(ops, update.session_id, slot, update.request_id, state)
        return await next_handler(event)

    @staticmethod
    async def _load_usage(
        ops: EventHandlerOps,
        session_id: str,
        request_id: str,
        state: SessionState,
    ) -> bool:
        usage = await ops.session.usage()
        if not isinstance(usage, Mapping) or usage.get("session_id") != session_id:
            return False
        turn_usage = usage.get("turn")
        if not isinstance(turn_usage, Mapping) or turn_usage.get("request_id") != request_id:
            return False
        state.turn(request_id).total_tokens = turn_usage.get("total_tokens")
        return True

    async def on_event(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.slot_id not in SLOTS:
            return await next_handler(event)
        target = request.target
        if target.slot_id == "application.entry":
            return await self._on_application_entry(request, ops, next_handler, event)
        if target.slot_id == "application.sidebar.footer":
            return await self._on_sidebar_footer(request, ops, next_handler, event)
        if target.package_id not in ("", PACKAGE_ID) or not target.session_id:
            return await next_handler(event)
        if request.event_type != "ui.ready" and target.package_id != PACKAGE_ID:
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")
        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.setdefault(target.session_id, SessionState())
            if request.event_type == "ui.ready":
                instances = self._instances(target)
                if not instances:
                    return UiActionOutput(accepted=False, status="invalid_instance")
                failed_status = None
                for instance in instances:
                    if target.slot_id in TURN_SLOTS and state.turn(instance).total_tokens is None:
                        await self._load_usage(ops, target.session_id, instance, state)
                    if target.slot_id == "conversation.panel.right" and instance == INSTANCE:
                        await load_workspace(ops, state.workspace)
                    output = await self._render(ops, target.session_id, target.slot_id, instance, state)
                    if output is None or output.status != "delivered":
                        failed_status = output.status if output else "unavailable"
                    else:
                        state.mounted.add((target.slot_id, instance))
                if failed_status:
                    return UiActionOutput(accepted=False, status=failed_status)
                return UiActionOutput(accepted=True, status="rendered")
            if target.slot_id == "conversation.panel.right" and target.instance_key != INSTANCE:
                return UiActionOutput(accepted=False, status="invalid_instance")
            action = request.payload.get("action_id")
            if action in (WEATHER_HIDE_ACTION, WEATHER_SHOW_ACTION):
                if (target.slot_id != "conversation.input.above" or target.instance_key != INSTANCE
                        or request.event_type != "ui.click"):
                    return UiActionOutput(accepted=False, status="invalid")
                state.weather_visible = action == WEATHER_SHOW_ACTION
                targets = {("conversation.input.above", INSTANCE)}
                if ("application.settings", INSTANCE) in state.mounted:
                    targets.add(("application.settings", INSTANCE))
                failed_status = None
                for slot, instance in sorted(targets):
                    output = await self._render(ops, target.session_id, slot, instance, state)
                    if output is None or output.status != "delivered":
                        failed_status = output.status if output else "unavailable"
                return UiActionOutput(accepted=failed_status is None, status=failed_status or "rendered")
            if isinstance(action, str) and action.startswith("workbench.files."):
                data = request.payload.get("data", {})
                if (target.slot_id != "conversation.panel.right" or target.instance_key != INSTANCE
                        or not isinstance(data, Mapping)
                        or not await apply_file_action(ops, state.workspace, action, request.event_type, data)):
                    return UiActionOutput(accepted=False, status="invalid")
                output = await self._render(ops, target.session_id, target.slot_id, INSTANCE, state)
                delivered = output is not None and output.status == "delivered"
                return UiActionOutput(accepted=delivered, status=output.status if output else "unavailable")
            result = self._apply(request, state)
            if result != "updated":
                return UiActionOutput(accepted=result == "composing", status=result)
            if target.slot_id in TURN_SLOTS and (
                action == "workbench.turn.refresh"
                or (action == TURN_SELECT_ACTION and request.payload.get("data", {}).get("key") == "refresh")
            ):
                await self._load_usage(ops, target.session_id, target.instance_key, state)
            state.operations += 1
            failed_status = None
            for slot, instance in sorted(state.mounted):
                if slot == "conversation.panel.right" and instance == IFRAME_INSTANCE:
                    continue
                if slot in IFRAME_PAGE_SLOTS:
                    continue
                if slot in TURN_SLOTS and instance != target.instance_key:
                    continue
                output = await self._render(ops, target.session_id, slot, instance, state)
                if output is None or output.status != "delivered":
                    failed_status = output.status if output else "unavailable"
            if failed_status:
                return UiActionOutput(accepted=False, status=failed_status)
            return UiActionOutput(accepted=True, status="rendered")

    async def _on_application_entry(
        self,
        request: UiActionInput,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
        event: Event,
    ) -> UiActionOutput:
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or target.session_id:
            return await next_handler(event)
        if request.event_type != "ui.ready" and target.package_id != PACKAGE_ID:
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")
        render_target = replace(target, package_id=PACKAGE_ID, instance_key=INSTANCE)
        if request.event_type == "ui.ready":
            if target.instance_key not in ("", INSTANCE):
                return UiActionOutput(accepted=False, status="invalid_instance")
            output = await ops.ui.render(UiRenderInput(target=render_target, root=welcome_page()))
            delivered = output is not None and output.status == "delivered"
            return UiActionOutput(accepted=delivered, status="rendered" if delivered else (output.status if output else "unavailable"))
        if (
            request.event_type == "ui.click"
            and target.instance_key == INSTANCE
            and request.payload.get("action_id") == "workbench.entry.close"
        ):
            output = await ops.ui.render(
                UiRenderInput(target=render_target, mode=RenderMode.CLEAR)
            )
            delivered = output is not None and output.status == "delivered"
            return UiActionOutput(accepted=delivered, status="cleared" if delivered else (output.status if output else "unavailable"))
        return UiActionOutput(accepted=False, status="invalid")

    async def _on_sidebar_footer(
        self,
        request: UiActionInput,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
        event: Event,
    ) -> UiActionOutput:
        """Render the application-scope user menu keyed by the browser connection."""
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or target.session_id:
            return await next_handler(event)
        if request.event_type != "ui.ready" and target.package_id != PACKAGE_ID:
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")
        connection = target.connection_id or "default"
        if request.event_type == "ui.ready":
            if target.instance_key not in ("", USER_INSTANCE):
                return UiActionOutput(accepted=False, status="invalid_instance")
            return await self._render_user_menu(ops, target, self._signed_in.get(connection, True))
        if target.instance_key != USER_INSTANCE:
            return UiActionOutput(accepted=False, status="invalid_instance")
        if request.event_type == "ui.select":
            data = request.payload.get("data")
            if (request.payload.get("action_id") != USER_SELECT_ACTION
                    or not isinstance(data, Mapping) or data.get("key") not in ("profile", "logout")):
                return UiActionOutput(accepted=False, status="invalid")
            if data["key"] == "profile":
                return UiActionOutput(accepted=True, status="selected")
            self._signed_in[connection] = False
            return await self._render_user_menu(ops, target, False)
        if request.event_type == "ui.click" and request.payload.get("action_id") == USER_LOGIN_ACTION:
            self._signed_in[connection] = True
            return await self._render_user_menu(ops, target, True)
        return UiActionOutput(accepted=False, status="invalid")

    @staticmethod
    async def _render_user_menu(ops: EventHandlerOps, target: RenderTarget, signed_in: bool) -> UiActionOutput:
        output = await ops.ui.render(UiRenderInput(
            target=replace(target, package_id=PACKAGE_ID, instance_key=USER_INSTANCE),
            root=user_menu_view() if signed_in else signed_out_view(),
        ))
        delivered = output is not None and output.status == "delivered"
        status = "rendered" if delivered else (output.status if output is not None else "unavailable")
        return UiActionOutput(accepted=delivered, status=status)

    @staticmethod
    def _instances(target: RenderTarget) -> tuple[str, ...]:
        if target.slot_id in TURN_SLOTS:
            return (target.instance_key,) if target.instance_key else ()
        if target.slot_id == "conversation.panel.right":
            members = (INSTANCE, IFRAME_INSTANCE)
            if not target.instance_key:
                return members
            return (target.instance_key,) if target.instance_key in members else ()
        expected = TASK_INSTANCE if target.slot_id in ("task.function", "task.function.main") else INSTANCE
        return (expected,) if target.instance_key in ("", expected) else ()

    @staticmethod
    def _apply(request: UiActionInput, state: SessionState) -> str:
        payload = request.payload
        action = payload.get("action_id")
        data = payload.get("data", {})
        args = payload.get("args", {})
        if not isinstance(data, Mapping) or not isinstance(args, Mapping):
            return "invalid"
        if request.target.slot_id in TURN_SLOTS:
            if action == TURN_SELECT_ACTION:
                if (request.target.slot_id != "conversation.turn.footer.actions"
                        or request.event_type != "ui.select" or data.get("key") not in ("refresh", "details")):
                    return "invalid"
                action = f"workbench.turn.{data['key']}"
            turn = state.turn(request.target.instance_key)
            if action == "workbench.turn.refresh":
                turn.summary = state.summary()
                turn.refreshes += 1
                turn.result = f"本轮刷新 / Round refresh: {turn.refreshes}"
            elif action == "workbench.turn.details":
                turn.result = f"{request.target.instance_key} · {turn.summary}"
            else:
                return "invalid"
            turn.result_visible = True
            return "updated"
        if action == "workbench.option" and args.get("field") == "weather_visible" and type(data.get("checked")) is bool:
            state.weather_visible = data["checked"]
        elif action not in ("workbench.open", "workbench.refresh", QUICK_LAUNCH_ACTION):
            return "invalid"
        state.last_result = f"已更新 / Updated · {state.operations + 1} · {state.summary()}"
        return "updated"

    @staticmethod
    async def _render(
        ops: EventHandlerOps,
        session_id: str,
        slot: str,
        instance: str,
        state: SessionState,
    ) -> UiRenderOutput | None:
        return await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id, slot, PACKAGE_ID, instance),
            root=build_view(slot, state, instance),
        ))
