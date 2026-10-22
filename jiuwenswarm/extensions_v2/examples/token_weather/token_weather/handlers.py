# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render the last-round usage line and the session context summary."""

from __future__ import annotations

import logging

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.events.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
)
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
)

from .state import SessionUsage, cache_rate, format_rate, format_tokens

logger = logging.getLogger(__name__)

PACKAGE_ID = "webui.token-weather"
SLOT_TURN = "message.assistant.meta"
SLOT_SUMMARY = "conversation.input.below"
INSTANCE_SUMMARY = "token-weather.summary"


class TokenWeatherHandlers:
    """One instance per activation; usage is scoped by session id."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionUsage] = {}

    def handlers(self):
        return (
            (CONTEXT_USAGE_EVENT, self.on_usage, {}),
            (WebUIEvent.READY.value, self.on_ready_turn, {"target.slot_id": SLOT_TURN}),
            (WebUIEvent.READY.value, self.on_ready, {"target.slot_id": SLOT_SUMMARY}),
        )

    async def on_usage(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> ContextUsageOutput:
        del next_handler
        update = event.event_input
        if not isinstance(update, ContextUsageUpdate):
            return ContextUsageOutput(accepted=False)
        state = self._state(update.session_id)
        if not state.apply(update):
            return ContextUsageOutput(accepted=True, request_id=update.request_id)
        await self._render_turn(ops, update)
        await self._render_summary(ops, update.session_id, state)
        return ContextUsageOutput(accepted=True, request_id=update.request_id)

    async def on_ready(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        del next_handler
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        state = self._sessions.get(request.target.session_id)
        if state is None or state.turns == 0:
            return UiActionOutput(accepted=True, status="idle")
        await self._render_summary(ops, request.target.session_id, state)
        return UiActionOutput(accepted=True, status="rendered")

    async def on_ready_turn(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        del next_handler
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        state = self._sessions.get(request.target.session_id)
        update = state.rounds.get(request.target.instance_key) if state is not None else None
        if update is None:
            return UiActionOutput(accepted=True, status="idle")
        await self._render_turn(ops, update)
        return UiActionOutput(accepted=True, status="rendered")

    def _state(self, session_id: str) -> SessionUsage:
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionUsage()
            self._sessions[session_id] = state
        return state

    async def _render_turn(
        self,
        ops: EventHandlerOps | None,
        update: ContextUsageUpdate,
    ) -> None:
        if ops is None:
            logger.warning("[TokenWeather] ops are unavailable; render skipped")
            return
        target = RenderTarget(
            session_id=update.session_id,
            slot_id=SLOT_TURN,
            package_id=PACKAGE_ID,
            instance_key=update.request_id,
        )
        await ops.ui.render(UiRenderInput(target=target, root=_turn_tree(update)))

    async def _render_summary(
        self,
        ops: EventHandlerOps | None,
        session_id: str,
        state: SessionUsage,
    ) -> None:
        if ops is None:
            logger.warning("[TokenWeather] ops are unavailable; render skipped")
            return
        summary_target = _target(session_id, SLOT_SUMMARY, INSTANCE_SUMMARY)
        await ops.ui.render(UiRenderInput(target=summary_target, root=_summary_tree(state)))


def _target(session_id: str, slot_id: str, instance_key: str) -> RenderTarget:
    return RenderTarget(
        session_id=session_id,
        slot_id=slot_id,
        package_id=PACKAGE_ID,
        instance_key=instance_key,
    )


def _turn_tree(update: ContextUsageUpdate) -> RenderNode:
    children = [
        RenderNode(
            "Text",
            {"value": format_tokens(update.total_tokens), "size": "sm", "variant": "secondary"},
        ),
    ]
    rate = cache_rate(update.cache_tokens, update.input_tokens)
    cache = format_rate(rate)
    if cache is not None:
        children.append(
            RenderNode(
                "Text",
                {"value": f"缓存 {cache}", "size": "sm", "tone": _cache_tone(rate)},
            )
        )
    return RenderNode("Row", {"gap": "sm", "density": "compact", "justify": "end"}, children=tuple(children))


def _summary_tree(state: SessionUsage) -> RenderNode:
    parts = [f"{state.turns} 轮", f"累计 {format_tokens(state.total_tokens)}"]
    rate = format_rate(state.cache_rate)
    if rate is not None:
        parts.append(f"缓存 {rate}")
    left = RenderNode(
        "Text",
        {"value": " · ".join(parts), "size": "sm", "variant": "secondary"},
    )

    right_parts: list[str] = []
    if state.usage_percent is not None:
        percent = max(0.0, min(100.0, state.usage_percent))
        right_parts.append(f"上下文 {percent:.0f}%")
    if state.context_window_tokens:
        right_parts.append(f"窗口 {format_tokens(state.context_window_tokens)}")
    right_value = " · ".join(right_parts)
    right = RenderNode(
        "Text",
        {
            "value": right_value,
            "size": "sm",
            "tone": _percent_tone(state.usage_percent) if state.usage_percent is not None else "neutral",
        },
    )
    children = [left]
    if right_value:
        children.append(right)
    return RenderNode("Row", {"gap": "md", "justify": "between", "density": "compact"}, children=tuple(children))


def _cache_tone(rate: float | None) -> str:
    if rate is None:
        return "neutral"
    if rate >= 0.8:
        return "success"
    if rate >= 0.5:
        return "info"
    return "neutral"


def _percent_tone(percent: float) -> str:
    if percent >= 90:
        return "danger"
    if percent >= 70:
        return "warning"
    return "info"
