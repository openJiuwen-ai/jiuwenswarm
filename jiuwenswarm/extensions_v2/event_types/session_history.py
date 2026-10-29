# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public transcript message contracts, validators and event registration."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

SESSION_APPEND_EVENT = "session.append"


@dataclass(frozen=True, slots=True)
class SessionAppendMessage:
    """A detached message view; only existing text blocks can be rewritten."""

    type: str
    content: list[dict[str, Any]]
    role: str | None = None
    name: str | None = None
    is_meta: bool = False
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return independent JSON-compatible message fields."""
        return copy.deepcopy(asdict(self))


@dataclass(frozen=True, slots=True)
class SessionAppendOrigin:
    """Host-pinned producer identity and actual persistence entrance."""

    kind: str
    name: str | None
    entry: str


@dataclass(frozen=True, slots=True)
class SessionAppendInput:
    """One pending row, its semantic door, producer and final row identity."""

    message: SessionAppendMessage
    door: str
    origin: SessionAppendOrigin
    uuid: str


@dataclass(frozen=True, slots=True)
class SessionAppendRejection:
    """Explicitly refuse an extension note before calling next_handler."""

    reason: str


@dataclass(frozen=True, slots=True)
class SessionAppendResult:
    """An actual commit receipt, or rejection without a committed row."""

    status: str
    session_id: str
    message_id: str
    message: SessionAppendMessage | None = None
    reason: str | None = None
    uuid: str | None = None
    history_record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached receipt without exposing mutable commit state."""
        return {
            "status": self.status, "session_id": self.session_id, "message_id": self.message_id,
            "message": self.message.to_dict() if self.message is not None else None,
            "reason": self.reason, "uuid": self.uuid, "history_record": copy.deepcopy(self.history_record),
        }


def valid_append_input(value: object) -> bool:
    """Validate content edits against the private host-owned invocation."""
    from jiuwenswarm.server.runtime.session.history_events import append_invocation
    from jiuwenswarm.server.runtime.session.history_message import valid_message_rewrite

    state = append_invocation()
    if state is None or not isinstance(value, SessionAppendInput):
        return False
    original = state.original
    return (
        (value.uuid, value.door, value.origin) == (original.uuid, original.door, original.origin)
        and valid_message_rewrite(original.message, value.message)
    )


def valid_append_output(value: object) -> bool:
    """Keep note refusal distinct from invalid or fabricated handler output."""
    from jiuwenswarm.server.runtime.session.history_events import append_invocation

    state = append_invocation()
    if state is None:
        return False
    own_note = state.original.origin.kind == "extension" and state.original.door == "note"
    if isinstance(value, SessionAppendRejection):
        return own_note and isinstance(value.reason, str) and 0 < len(value.reason.strip()) <= 512
    if not isinstance(value, SessionAppendResult):
        return False
    if not own_note and state.receipt is None:
        return False
    return value.status in ("persisted", "duplicate", "rejected") and (own_note or value.status != "rejected")


def register_session_history_events(bus: ExtensionEventBus) -> None:
    """Register the public event and the platform persistence CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_history import session_append_core

    bus.register_event_type(
        SESSION_APPEND_EVENT, match_pattern_subset, session_append_core,
        input_validator=valid_append_input, output_validator=valid_append_output,
    )
