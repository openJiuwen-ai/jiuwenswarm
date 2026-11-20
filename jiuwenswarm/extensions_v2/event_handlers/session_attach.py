# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Acknowledge existing associations without switching or starting a Session."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_attach import (
    SessionAttachOutput,
    current_attach_invocation,
    valid_attach_input,
)


async def session_attach_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionAttachOutput:
    """Confirm only the current target and return its actual receipt."""
    del next_handler
    state = current_attach_invocation()
    if not valid_attach_input(event.event_input):
        raise ValueError("session.attach input cannot be changed")
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.session_id:
        raise RuntimeError("session.attach requires its bound SessionContext")
    if not await state.validate_target():
        raise RuntimeError("session.attach target is no longer available")
    state = current_attach_invocation()
    if state.receipt is None:
        state.receipt = SessionAttachOutput(state.original.clientId)
    return state.receipt
