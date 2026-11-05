# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Echo the ending session; Runtime remains the resource-release owner."""

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
    """Confirm the real ending run without deleting its persisted transcript."""
    del next_handler
    state = current_end_invocation()
    if not valid_end_input(event.event_input):
        raise ValueError("session.end input cannot be changed")
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.original.sessionId:
        raise RuntimeError("session.end requires its bound SessionContext")
    if state.receipt is None:
        state.receipt = SessionEndOutput(state.original.sessionId)
    return state.receipt
