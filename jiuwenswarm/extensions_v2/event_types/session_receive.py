# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude-aligned incoming delivery contracts and event registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_delivery import valid_delivery_reason, valid_delivery_text

SESSION_RECEIVE_EVENT = "session.receive"


@dataclass(frozen=True, slots=True)
class SessionReceiveOrigin:
    """Source classification asserted by the host, never inferred from text."""

    kind: str


@dataclass(frozen=True, slots=True)
class SessionReceiveEvent:
    """A host-asserted external event; ordinary peer messages have none."""

    source: str
    kind: str
    data: dict[str, Any]
    untrusted_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionReceiveInput:
    """An incoming body whose origin and structured event are pinned."""

    origin: SessionReceiveOrigin
    text: str
    event: SessionReceiveEvent | None = None


@dataclass(frozen=True, slots=True)
class SessionReceiveResult:
    """The actual text accepted by the durable mailbox."""

    text: str


@dataclass(frozen=True, slots=True)
class SessionReceiveConsumed:
    """Take an incoming delivery without queueing or model execution."""

    consumed: str


def valid_receive_input(value: object) -> bool:
    """Accept body edits without changing any asserted source facts."""
    from jiuwenswarm.server.runtime.session.session_message_events import receive_invocation

    state = receive_invocation()
    return bool(
        state is not None and state.active and isinstance(value, SessionReceiveInput)
        and (value.origin, value.event) == (state.original.origin, state.original.event)
        and valid_delivery_text(value.text)
    )


def valid_receive_output(value: object) -> bool:
    """Distinguish a real queue receipt from a pre-queue consumption."""
    from jiuwenswarm.server.runtime.session.session_message_events import receive_invocation

    state = receive_invocation()
    if state is None or not state.active:
        return False
    if isinstance(value, SessionReceiveConsumed):
        return not state.attempted and state.receipt is None and valid_delivery_reason(value.consumed)
    return isinstance(value, SessionReceiveResult) and state.receipt is not None and value == state.result


def register_session_receive_event(bus: ExtensionEventBus) -> None:
    """Register the incoming boundary; the CORE commits through the host."""
    from jiuwenswarm.extensions_v2.event_handlers.session_receive import session_receive_core

    bus.register_event_type(
        SESSION_RECEIVE_EVENT, match_pattern_subset, session_receive_core,
        input_validator=valid_receive_input, output_validator=valid_receive_output,
    )
