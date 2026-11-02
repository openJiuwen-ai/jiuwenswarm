# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default outgoing action: continue to the target's receive boundary once."""

import copy

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_send import valid_send_input
from jiuwenswarm.server.runtime.session.session_message_events import require_delivery_invocation


async def session_send_core(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> dict:
    """Execute the authorized host continuation and return its actual receipt."""
    del next_handler
    state = require_delivery_invocation("session.send", ops)
    if not valid_send_input(event.event_input):
        raise ValueError("invalid session.send input")
    async with state.lock:
        state.require_active()
        if state.failure is not None:
            raise state.failure
        if state.receipt is None:
            state.attempted = True
            try:
                state.receipt = copy.deepcopy(await state.commit(event.event_input.text.strip()))
            except BaseException as failure:
                state.failure = failure
                raise
        return copy.deepcopy(state.receipt)
