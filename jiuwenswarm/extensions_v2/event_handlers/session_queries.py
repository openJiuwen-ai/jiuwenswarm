# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default platform readers and optional query-chain observability wrappers."""

from __future__ import annotations

import logging
from time import perf_counter

from openjiuwen.core.events import Event, EventHandlerOps, EventNext, ExtensionEventBus, HandlerLevel

from jiuwenswarm.extensions_v2.event_types.session_queries import SESSION_QUERY_EVENTS
from jiuwenswarm.extensions_v2.query_dispatch import (
    current_query_origin, detached_copy, require_query_context,
)
from jiuwenswarm.server.runtime.session.query_views import read_session_query

logger = logging.getLogger(__name__)


async def session_query_core(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> dict:
    """Answer from trusted platform readers, never from another public query Ops."""
    del next_handler
    context = require_query_context(ops)
    return {"value": await read_session_query(event.event_type, event.event_input, context)}


async def session_query_builtin(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> object:
    """Time a continued query and return an independent result without logging data."""
    del ops
    started = perf_counter()
    origin = current_query_origin()
    try:
        return detached_copy(await next_handler(event))
    finally:
        logger.debug("Session query event=%s caller=%s elapsed_ms=%.3f", event.event_type,
                     None if origin is None else origin.package, (perf_counter() - started) * 1000)


def register_session_query_handlers(bus: ExtensionEventBus) -> None:
    """Attach the same real BUILTIN observer to each registered query event."""
    for event_type in SESSION_QUERY_EVENTS:
        bus.register_event_handler(event_type, session_query_builtin, {}, HandlerLevel.BUILTIN)
