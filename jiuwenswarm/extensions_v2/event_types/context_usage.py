# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host event that publishes per-turn token usage after a chat round."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from openjiuwen.core.events import (
    Event,
    EventHandlerOps,
    EventNext,
    ExtensionEventBus,
    SessionContext,
)

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

logger = logging.getLogger(__name__)

CONTEXT_USAGE_EVENT = "context.usage.updated"

_PENDING: set[asyncio.Task] = set()


@dataclass(frozen=True, slots=True)
class ContextUsageUpdate:
    """One finished chat round; values come from ``chat.usage_summary``."""

    session_id: str
    request_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    cache_hit_rate: str | None = None
    context_window_tokens: int | None = None
    usage_percent: float | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ContextUsageOutput:
    """Receipt returned by the CORE handler when no plugin consumes the event."""

    accepted: bool
    request_id: str = ""


async def context_usage_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> ContextUsageOutput:
    """Validate the payload; rendering is a plugin concern."""

    del ops, next_handler
    update = event.event_input
    if not isinstance(update, ContextUsageUpdate):
        raise TypeError(f"{CONTEXT_USAGE_EVENT} requires ContextUsageUpdate")
    return ContextUsageOutput(accepted=True, request_id=update.request_id)


def register_context_usage_events(bus: ExtensionEventBus) -> None:
    """Register the host event type and its CORE handler."""

    bus.register_event_type(CONTEXT_USAGE_EVENT, match_pattern_subset, context_usage_core)


def notify_context_usage(update: ContextUsageUpdate) -> asyncio.Task | None:
    """Schedule the host event without blocking the chat stream."""

    if not update.session_id:
        return None
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus

    bus = get_extension_bus()
    if bus is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_dispatch(bus, update))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def _dispatch(bus: ExtensionEventBus, update: ContextUsageUpdate) -> None:
    try:
        await bus.trigger(
            Event(CONTEXT_USAGE_EVENT, update),
            SessionContext(session_id=update.session_id),
        )
    except Exception:  # noqa: BLE001 - telemetry must not break the round
        logger.debug("[ExtensionsV2] context usage event failed", exc_info=True)
