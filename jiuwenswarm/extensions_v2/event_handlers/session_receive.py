# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default incoming action: commit the permitted text to the real mailbox."""

import copy

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_receive import SessionReceiveResult, valid_receive_input
from jiuwenswarm.server.runtime.session.session_message_events import require_delivery_invocation


async def session_receive_core(
    event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
) -> SessionReceiveResult:
    """Queue once and remember the real body before returning to user hooks."""
    del next_handler
    state = require_delivery_invocation("session.receive", ops)
    if not valid_receive_input(event.event_input):
        raise ValueError("invalid session.receive input")
    async with state.lock:
        state.require_active()
        if state.receipt is None:
            text = event.event_input.text.strip()
            state.receipt = copy.deepcopy(await state.commit(text))
            state.result = SessionReceiveResult(text=text)
        return copy.deepcopy(state.result)
