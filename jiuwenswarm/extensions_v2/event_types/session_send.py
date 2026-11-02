# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sender-side delivery contracts, validation and registration."""

from __future__ import annotations

from dataclasses import dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_delivery import valid_delivery_reason, valid_delivery_text

SESSION_SEND_EVENT = "session.send"


@dataclass(frozen=True, slots=True)
class SessionSendInput:
    """One outgoing text with its host-pinned destination and delivery mode."""

    target_session_id: str
    text: str
    input_mode: str


@dataclass(frozen=True, slots=True)
class SessionSendDenied:
    """Refuse an outgoing delivery before calling next_handler."""

    deny: str


def valid_send_input(value: object) -> bool:
    """Allow only body edits inside a real authorized platform invocation."""
    from jiuwenswarm.server.runtime.session.session_message_events import send_invocation

    state = send_invocation()
    return bool(
        state is not None and state.active and isinstance(value, SessionSendInput)
        and (value.target_session_id, value.input_mode)
        == (state.original.target_session_id, state.original.input_mode)
        and valid_delivery_text(value.text)
    )


def valid_send_output(value: object) -> bool:
    """Require a real receipt or a pre-commit explicit refusal."""
    from jiuwenswarm.server.runtime.session.session_message_events import send_invocation

    state = send_invocation()
    if state is None or not state.active:
        return False
    if isinstance(value, SessionSendDenied):
        return not state.attempted and state.receipt is None and valid_delivery_reason(value.deny)
    return isinstance(value, dict) and state.receipt is not None and value == state.receipt


def register_session_send_event(bus: ExtensionEventBus) -> None:
    """Register the outgoing policy boundary and its platform CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_send import session_send_core

    bus.register_event_type(
        SESSION_SEND_EVENT, match_pattern_subset, session_send_core,
        input_validator=valid_send_input, output_validator=valid_send_output,
    )
