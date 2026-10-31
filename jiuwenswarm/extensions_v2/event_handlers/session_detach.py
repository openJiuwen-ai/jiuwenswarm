# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Confirm a released view association without changing Session resources."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_detach import (
    SessionDetachOutput,
    current_detach_invocation,
    valid_detach_input,
)


async def session_detach_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionDetachOutput:
    """Return a real receipt for the old identity, never release it again."""
    del next_handler
    state = current_detach_invocation()
    if not valid_detach_input(event.event_input):
        raise ValueError("session.detach input cannot be changed")
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.original.session_id:
        raise RuntimeError("session.detach requires the old bound SessionContext")
    if state.receipt is None:
        value = state.original
        state.receipt = SessionDetachOutput(True, value.session_id, value.surface_id, value.attachment_id)
    return state.receipt
