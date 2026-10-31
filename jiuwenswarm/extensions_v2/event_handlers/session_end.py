# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default acknowledgement; Runtime remains the sole resource-release owner."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_end import (
    SessionEndOutput,
    current_end_invocation,
    valid_end_input,
)


async def session_end_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionEndOutput:
    """Acknowledge the real target without invoking deletion a second time."""
    del next_handler
    state = current_end_invocation()
    if not valid_end_input(event.event_input):
        raise ValueError("session.end input cannot be changed")
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.original.session_id:
        raise RuntimeError("session.end requires its bound SessionContext")
    if state.receipt is None:
        state.receipt = SessionEndOutput(True, state.original.session_id, state.original.end_id)
    return state.receipt
