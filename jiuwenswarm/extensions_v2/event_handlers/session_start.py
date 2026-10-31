# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default session.start receipt for a host-selected activation."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_start import (
    SessionStartOutput,
    current_start_invocation,
    valid_start_input,
)


async def session_start_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionStartOutput:
    """Confirm a notification without creating resources or another input."""
    del next_handler
    state = current_start_invocation()
    value = event.event_input
    if not valid_start_input(value):
        raise ValueError("session.start input cannot be rewritten")
    if ops is None or ops.session_context is None or ops.session_context.session_id != value.session_id:
        raise ValueError("session.start requires its bound session")
    if state.receipt is None:
        state.receipt = SessionStartOutput(True, value.package_id, value.generation,
                                          value.session_id, value.request_id)
    return state.receipt
