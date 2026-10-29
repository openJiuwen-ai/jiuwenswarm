# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted Worker notification boundary before product-history persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
import copy
from dataclasses import dataclass, replace
import hashlib
import math
from typing import Any
import uuid

from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.extensions_v2.event_types.session_history import (
    SESSION_APPEND_EVENT, SessionAppendInput, SessionAppendMessage, SessionAppendRejection, SessionAppendResult,
)
from jiuwenswarm.server.runtime.session.history_message import classify_record, message_from_record, prepare_platform_history
from jiuwenswarm.server.runtime.session.session_history import PreparedHistoryRecord
from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError, original_fingerprint, preflight_append


@dataclass
class AppendInvocation:
    """Host-owned state separate from the payload handed to extensions."""

    prepared: PreparedHistoryRecord
    original: SessionAppendInput
    message_id: str
    append_key: str
    fingerprint: str
    update_metadata: bool
    receipt: SessionAppendResult | None = None


_INVOCATION: ContextVar[AppendInvocation | None] = ContextVar("session_append_invocation", default=None)


def append_invocation() -> AppendInvocation | None:
    """Return private dispatch state for validators and the platform CORE."""
    return _INVOCATION.get()


def current_append_invocation() -> AppendInvocation:
    """Require the real platform entrance before allowing persistence."""
    state = append_invocation()
    if state is None:
        raise SessionHistoryError("APPEND_CONTEXT_REQUIRED")
    return state


async def dispatch_session_append(
    bus: ExtensionEventBus, context: SessionContext, prepared: PreparedHistoryRecord,
    *, message_id: str, source: str, append_key: str, fingerprint: str, update_metadata: bool,
    native_message: SessionAppendMessage | None = None,
) -> SessionAppendResult:
    """Await the policy chain and one real commit with trusted session facts."""
    if append_invocation() is not None:
        raise SessionHistoryError("APPEND_REENTRANT")
    if context.session_id != prepared.session_id:
        raise SessionHistoryError("INVALID_SESSION")
    generation = await asyncio.to_thread(preflight_append, prepared.session_id, require_existing=source == "extension")
    row_uuid = prepared.record["id"] if source == "extension" else "row:" + hashlib.sha256(append_key.encode("utf-8")).hexdigest()
    prepared = replace(prepared, record={**prepared.record, "uuid": row_uuid}, metadata={**prepared.metadata, "append_generation": generation})
    model_info = (context.metadata or {}).get("session_model_info")
    model_name = model_info.get("model_name") if isinstance(model_info, Mapping) else None
    door, origin = classify_record(prepared.record, source, model_name)
    message = native_message if native_message is not None else message_from_record(prepared.record, prefer_stored=source == "extension")
    original = SessionAppendInput(copy.deepcopy(message), door, origin, row_uuid)
    state = AppendInvocation(copy.deepcopy(prepared), copy.deepcopy(original), message_id, append_key, fingerprint, update_metadata)
    token = _INVOCATION.set(state)
    try:
        result = await bus.trigger(Event(SESSION_APPEND_EVENT, copy.deepcopy(original)), context)
        if state.receipt is not None:
            return copy.deepcopy(state.receipt)
        if isinstance(result, SessionAppendRejection) and origin.kind == "extension" and door == "note":
            return SessionAppendResult("rejected", prepared.session_id, message_id, reason=result.reason.strip())
        raise SessionHistoryError("APPEND_RESULT_INVALID")
    finally:
        _INVOCATION.reset(token)


async def try_append_platform_history(
    record_kwargs: Mapping[str, Any], *, source: str = "history", idempotency_key: str | None = None,
) -> tuple[bool, SessionAppendResult | None]:
    """Bridge supported awaitable main-history entrances to Worker EventBus."""
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus
    from jiuwenswarm.server.runtime.session.history_context import platform_history_context

    bus = get_extension_bus()
    if bus is None or record_kwargs.get("subagent_id") or is_team_mode(record_kwargs.get("mode", "")) or record_kwargs.get("mode") == "subagent":
        return False, None
    prepared, native_message = prepare_platform_history(record_kwargs)
    if prepared is None:
        return True, None
    if not math.isfinite(prepared.metadata["timestamp"]) or prepared.metadata["timestamp"] <= 0:
        raise SessionHistoryError("INVALID_MESSAGE")
    append_key = "platform:" + hashlib.sha256((idempotency_key or uuid.uuid4().hex).encode("utf-8")).hexdigest()
    context = await platform_history_context(prepared.session_id)
    result = await dispatch_session_append(
        bus, context, prepared, message_id=prepared.record["id"], source=source, append_key=append_key,
        fingerprint=original_fingerprint(prepared.record), update_metadata=True, native_message=native_message,
    )
    return True, result
