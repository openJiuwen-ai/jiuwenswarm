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
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.session_id or context.cwd != value.cwd:
        raise ValueError("session.start requires its bound session")
    if state.receipt is None:
        state.receipt = SessionStartOutput(value.cwd)
    return state.receipt
