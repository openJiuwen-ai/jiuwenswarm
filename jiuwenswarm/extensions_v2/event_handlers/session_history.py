# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default session.append behavior: validate, commit, and return real facts."""

from __future__ import annotations

import copy
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_history import (
    SessionAppendResult,
    current_append_invocation,
    valid_append_input,
)
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError, commit_session_message


async def session_append_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionAppendResult:
    """Persist once per invocation after all earlier handlers have finished."""
    del next_handler
    state = current_append_invocation()
    if ops is None or ops.session_context is None or ops.session_context.session_id != state.prepared.session_id:
        raise SessionHistoryError("INVALID_SESSION")
    if not valid_append_input(event.event_input):
        raise SessionHistoryError("INVALID_MESSAGE")
    if state.receipt is not None:
        return copy.deepcopy(state.receipt)
    content = event.event_input.content
    prepared = replace(
        state.prepared,
        record={**state.prepared.record, "content": content},
        metadata={**state.prepared.metadata, "content": content},
    )
    record, duplicate = await run_history_io(
        commit_session_message, prepared, state.append_key, state.fingerprint, state.update_metadata,
    )
    state.receipt = SessionAppendResult(
        "duplicate" if duplicate else "persisted", prepared.session_id, state.original.message_id, record,
    )
    return copy.deepcopy(state.receipt)
