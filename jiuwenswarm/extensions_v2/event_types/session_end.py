# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-owned input and receipt for an ending Runtime session run."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.runtime.session_lifecycle import SessionRunEndReason
from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

SESSION_END_EVENT = "session.end"


@dataclass(frozen=True, slots=True)
class SessionResume:
    """The persisted identity a caller may resume, if it has saved history."""

    id: str


@dataclass(frozen=True, slots=True)
class SessionEndInput:
    """Immutable engine facts, matching Claude reason/sessionId/resume."""

    reason: str
    session_id: str
    resume: SessionResume


@dataclass(frozen=True, slots=True)
class SessionEndOutput:
    """The actual ending session identity echoed by CORE."""

    session_id: str


@dataclass
class EndInvocation:
    """Host validation state that becomes unusable after timeout or return."""

    original: SessionEndInput
    owner: asyncio.Task | None = None
    active: bool = True
    receipt: SessionEndOutput | None = None


_INVOCATION: ContextVar[EndInvocation | None] = ContextVar("session_end_invocation", default=None)


def current_end_invocation() -> EndInvocation:
    """Require this task's still-active host notification scope."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task():
        raise RuntimeError("session.end requires a trusted end invocation")
    return state


def valid_end_input(value: object) -> bool:
    """Allow observations of the original input, never rewritten targets."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and type(value) is SessionEndInput and value == state.original
        and type(value.resume) is SessionResume and value.resume.id == value.session_id
        and value.reason in {reason.value for reason in SessionRunEndReason}
    )


def valid_end_output(value: object) -> bool:
    """Only the actual CORE receipt can complete the notification chain."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and state.receipt is not None and type(value) is SessionEndOutput
        and value == state.receipt
    )


@contextmanager
def bind_end_invocation(state: EndInvocation) -> Iterator[None]:
    """Bind one host state to its dispatch task until completion or timeout."""
    if not state.active or state.owner is not None:
        raise RuntimeError("session.end invocation is no longer available")
    state.owner = asyncio.current_task()
    token = _INVOCATION.set(state)
    try:
        yield
    finally:
        state.active = False
        _INVOCATION.reset(token)


def register_session_end_event(bus: ExtensionEventBus) -> None:
    """Register the terminal notification type, validators and default CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_end import session_end_core

    bus.register_event_type(
        SESSION_END_EVENT, match_pattern_subset, session_end_core,
        input_validator=valid_end_input, output_validator=valid_end_output,
    )
