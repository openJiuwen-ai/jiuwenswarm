# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session compaction public values, validators and registration."""

from dataclasses import dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_queries import _messages

SESSION_COMPACT_EVENT = "session.compact"


@dataclass(frozen=True, slots=True)
class SessionCompactInput:
    trigger: str
    messages: list[dict]
    agent_id: str | None = None
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCompacted:
    messages: list[dict]
    tokens_before: int | None = None
    tokens_after: int | None = None


@dataclass(frozen=True, slots=True)
class SessionCompactSkipped:
    skip: str


def valid_compact_messages(messages) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        if "handle" in message and (not isinstance(message["handle"], str) or not message["handle"]):
            return False
        if not _messages([{key: value for key, value in message.items() if key != "handle"}]):
            return False
    return True


def valid_compact_input(value) -> bool:
    from jiuwenswarm.extensions_v2.session_compaction.dispatch import compact_invocation
    frame = compact_invocation()
    valid = bool(
        frame is not None and isinstance(value, SessionCompactInput)
        and (value.trigger, value.agent_id) == (frame.original.trigger, frame.original.agent_id)
        and (value.instructions is None or isinstance(value.instructions, str))
        and valid_compact_messages(value.messages)
    )
    if not valid:
        return False
    try:
        frame.codec.decode(value.messages)
    except (ValueError, TypeError, RuntimeError):
        return False
    return True


def valid_compact_output(value) -> bool:
    from jiuwenswarm.extensions_v2.session_compaction.dispatch import compact_invocation
    frame = compact_invocation()
    if frame is None:
        return False
    if isinstance(value, SessionCompactSkipped):
        return isinstance(value.skip, str) and 1 <= len(value.skip.strip()) <= 512
    if not isinstance(value, SessionCompacted) or not valid_compact_messages(value.messages):
        return False
    if not all(number is None or type(number) is int and number >= 0 for number in (value.tokens_before, value.tokens_after)):
        return False
    try:
        frame.codec.decode(value.messages)
    except (ValueError, TypeError, RuntimeError):
        return False
    return True


def register_session_compact_event(bus: ExtensionEventBus) -> None:
    from jiuwenswarm.extensions_v2.event_handlers.session_compact import session_compact_core
    bus.register_event_type(
        SESSION_COMPACT_EVENT, match_pattern_subset, session_compact_core,
        input_validator=valid_compact_input, output_validator=valid_compact_output,
    )
