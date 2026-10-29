# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default session.append behavior: validate, commit, and return real facts."""

from __future__ import annotations

import copy

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.session_history import (
    SessionAppendResult,
    valid_append_input,
)
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_events import current_append_invocation
from jiuwenswarm.server.runtime.session.history_message import apply_message, message_from_record
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
    prepared = apply_message(state.prepared, event.event_input.message, state.original.message)
    record, duplicate = await run_history_io(
        commit_session_message, prepared, state.append_key, state.fingerprint, state.update_metadata,
    )
    state.receipt = SessionAppendResult(
        "duplicate" if duplicate else "persisted", prepared.session_id, state.message_id,
        message_from_record(record), uuid=record.get("uuid", record["id"]), history_record=record,
    )
    return copy.deepcopy(state.receipt)
