# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pre-commit transcript event and trusted per-invocation dispatch boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
import copy
from dataclasses import dataclass, replace
import hashlib
import math
import uuid
from typing import Any

from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.server.runtime.session.session_history import PreparedHistoryRecord, prepare_history_record
from jiuwenswarm.server.runtime.session.session_history_ops import (
    MAX_APPEND_BYTES,
    SessionHistoryError,
    original_fingerprint,
    preflight_append,
)

SESSION_APPEND_EVENT = "session.append"


@dataclass(frozen=True, slots=True)
class SessionAppendInput:
    """Public append facts; only content may change before forwarding."""

    message_id: str
    role: str
    content: str
    source: str


@dataclass(frozen=True, slots=True)
class SessionAppendRejection:
    """An explicit rejection of an extension note before calling next_handler."""

    reason: str


@dataclass(frozen=True, slots=True)
class SessionAppendResult:
    """The real write outcome, or an explicit rejection without a write."""

    status: str
    session_id: str
    message_id: str
    message: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return an independent public receipt."""
        return {"status": self.status, "session_id": self.session_id, "message_id": self.message_id,
                "message": copy.deepcopy(self.message), "reason": self.reason}


@dataclass
class AppendInvocation:
    """Host-owned command state, never taken from mutable event input."""

    prepared: PreparedHistoryRecord
    original: SessionAppendInput
    append_key: str
    fingerprint: str
    update_metadata: bool
    receipt: SessionAppendResult | None = None


_INVOCATION: ContextVar[AppendInvocation | None] = ContextVar("session_append_invocation", default=None)


def current_append_invocation() -> AppendInvocation:
    """Require the trusted platform boundary used by the append CORE."""
    state = _INVOCATION.get()
    if state is None:
        raise SessionHistoryError("APPEND_CONTEXT_REQUIRED")
    return state


def valid_append_input(value: object) -> bool:
    """Reject routing changes and invalid rewritten bodies at next boundaries."""
    state = _INVOCATION.get()
    if state is None or not isinstance(value, SessionAppendInput):
        return False
    original = state.original
    if (value.message_id, value.role, value.source) != (original.message_id, original.role, original.source):
        return False
    if not isinstance(value.content, str):
        return False
    if original.content.strip() and not value.content.strip():
        return False
    try:
        return len(value.content.encode("utf-8")) <= max(MAX_APPEND_BYTES, len(original.content.encode("utf-8")))
    except UnicodeError:
        return False


def valid_append_output(value: object) -> bool:
    """Keep explicit rejections separate from malformed handler results."""
    if isinstance(value, SessionAppendRejection):
        state = _INVOCATION.get()
        return (
            state is not None and state.original.source == "extension"
            and isinstance(value.reason, str) and 0 < len(value.reason.strip()) <= 512
        )
    if isinstance(value, SessionAppendResult) and value.status == "rejected":
        state = _INVOCATION.get()
        return state is not None and state.original.source == "extension"
    if isinstance(value, SessionAppendResult):
        state = _INVOCATION.get()
        if state is not None and state.original.source != "extension" and state.receipt is None:
            return False
    return isinstance(value, SessionAppendResult) and value.status in ("persisted", "duplicate", "rejected")


async def dispatch_session_append(
    bus: ExtensionEventBus,
    context: SessionContext,
    prepared: PreparedHistoryRecord,
    *,
    message_id: str,
    source: str,
    append_key: str,
    fingerprint: str,
    update_metadata: bool,
) -> SessionAppendResult:
    """Await policy and persistence without exposing mutable target routing."""
    if _INVOCATION.get() is not None:
        raise SessionHistoryError("APPEND_REENTRANT")
    if context.session_id != prepared.session_id:
        raise SessionHistoryError("INVALID_SESSION")
    generation = await asyncio.to_thread(preflight_append, prepared.session_id, require_existing=source == "extension")
    prepared = replace(prepared, metadata={**prepared.metadata, "append_generation": generation})
    original = SessionAppendInput(message_id, prepared.record["role"], prepared.record["content"], source)
    state = AppendInvocation(copy.deepcopy(prepared), original, append_key, fingerprint, update_metadata)
    token = _INVOCATION.set(state)
    try:
        result = await bus.trigger(Event(SESSION_APPEND_EVENT, original), context)
        # A handler cannot undo a completed write by replacing its returned receipt.
        if state.receipt is not None:
            return copy.deepcopy(state.receipt)
        if isinstance(result, SessionAppendRejection):
            if source != "extension":
                raise SessionHistoryError("APPEND_RESULT_INVALID")
            return SessionAppendResult("rejected", prepared.session_id, message_id, reason=result.reason.strip())
        raise SessionHistoryError("APPEND_RESULT_INVALID")
    finally:
        _INVOCATION.reset(token)


async def try_append_platform_history(
    record_kwargs: Mapping[str, Any],
    *,
    source: str = "history",
    idempotency_key: str | None = None,
) -> tuple[bool, SessionAppendResult | None]:
    """Route existing awaitable main-history writes through the Worker bus.

    False preserves the original path when V2 is unavailable or this is a
    Team/subagent write. A filtered ephemeral record has no event or receipt.
    """
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus

    bus = get_extension_bus()
    if bus is None or record_kwargs.get("subagent_id") or is_team_mode(record_kwargs.get("mode", "")):
        return False, None
    if record_kwargs.get("mode") == "subagent":
        return False, None
    prepared = prepare_history_record(**record_kwargs)
    if prepared is None:
        return True, None
    if not math.isfinite(prepared.metadata["timestamp"]) or prepared.metadata["timestamp"] <= 0:
        raise SessionHistoryError("INVALID_MESSAGE")
    key = idempotency_key or uuid.uuid4().hex
    append_key = "platform:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    from jiuwenswarm.server.runtime.session.history_context import platform_history_context

    context = await platform_history_context(prepared.session_id)
    result = await dispatch_session_append(
        bus, context, prepared,
        message_id=prepared.record["id"], source=source, append_key=append_key,
        fingerprint=original_fingerprint(prepared.record), update_metadata=True,
    )
    return True, result


def register_session_history_events(bus: ExtensionEventBus) -> None:
    """Register the append event, validators and platform persistence CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_history import session_append_core

    bus.register_event_type(
        SESSION_APPEND_EVENT, match_pattern_subset, session_append_core,
        input_validator=valid_append_input, output_validator=valid_append_output,
    )
