# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Handle ready and component events; send new trees through ops.ui.render."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.webui.models import (
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

from .state import DEMO_ITEMS, SessionState
from .views import (
    IFRAME_INSTANCE,
    IFRAME_PAGE_SLOTS,
    INSTANCE,
    PACKAGE_ID,
    SLOTS,
    TASK_INSTANCE,
    TURN_SLOTS,
    build_view,
)
from .workspace import apply_file_action, load_workspace

EVENTS = ("ui.click", "ui.change", "ui.press_enter", "ui.search", "ui.clear", "ui.select")


class WorkbenchHandlers:
    """Keep session and round state inside one extension activation."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def handlers(self) -> tuple:
        return tuple(
            (event, self.on_event, {"target.slot_id": slot})
            for slot in SLOTS for event in ("ui.ready", *EVENTS)
        )

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
        value = data.get("value")
        if request.target.slot_id in TURN_SLOTS:
            turn = state.turn(request.target.instance_key)
            if action == "workbench.turn.refresh":
                turn.summary = state.summary()
                turn.refreshes += 1
                turn.result = f"本轮刷新 / Round refresh: {turn.refreshes}"
            elif action == "workbench.turn.details":
                turn.result = f"{request.target.instance_key} · {turn.summary}"
            else:
                return "invalid"
            return "updated"
        if action == "workbench.query" and isinstance(value, str) and len(value) <= 80:
            if data.get("isComposing") is True:
                return "composing"
            state.query = value
        elif action == "workbench.category" and value in ("design", "test"):
            state.category = value
        elif action == "workbench.category.clear":
            state.category = None
        elif action == "workbench.project" and value in (None, "platform", "examples"):
            state.project = value
        elif action == "workbench.project.clear":
            state.project = None
        elif action == "workbench.threshold" and type(value) in (int, float) and 0 <= value <= 100 and value % 5 == 0:
            state.threshold = int(value)
        elif action == "workbench.option" and args.get("field") in ("auto_summary", "destroy_hidden") and type(data.get("checked")) is bool:
            setattr(state, args["field"], data["checked"])
        elif action == "workbench.tab" and data.get("activeKey") in ("form", "results"):
            state.active_tab = data["activeKey"]
        elif action == "workbench.search" and isinstance(data.get("searchValue"), str):
            state.search_hint = data["searchValue"][:80]
        elif action == "workbench.details" and args.get("item") in {item.key for item in DEMO_ITEMS}:
            state.selected = args["item"]
        elif action == "workbench.image":
            state.image_failure = not state.image_failure
        elif action == "workbench.reset":
            state.reset()
        elif action == "workbench.apply":
            if request.event_type == "ui.press_enter" and isinstance(value, str) and len(value) <= 80:
                state.query = value
        elif action not in ("workbench.open", "workbench.refresh"):
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
