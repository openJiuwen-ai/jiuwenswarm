from __future__ import annotations

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
    register_context_usage_events,
)


@pytest.mark.asyncio
async def test_core_handler_accepts_usage_update() -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    output = await bus.trigger(
        Event(
            CONTEXT_USAGE_EVENT,
            ContextUsageUpdate(session_id="s1", request_id="r1", input_tokens=10, total_tokens=10),
        )
    )

    assert output == ContextUsageOutput(accepted=True, request_id="r1")


@pytest.mark.asyncio
async def test_core_handler_rejects_unknown_payload() -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    with pytest.raises(TypeError):
        await bus.trigger(Event(CONTEXT_USAGE_EVENT, object()))
