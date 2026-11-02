# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only facts for a view association already released by the host."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderSurface, valid_surface

SESSION_DETACH_EVENT = "session.detach"


@dataclass(frozen=True, slots=True)
class SessionDetachInput:
    """Identity of the released association, distinct from its replacement."""

    surface: RenderSurface
    clientId: str
    reason: str


@dataclass(frozen=True, slots=True)
class SessionDetachOutput:
    """CORE acknowledgement of the already committed release."""

    clientId: str


@dataclass
class DetachInvocation:
    """One host-owned notification scope, invalidated on cancellation or exit."""

    original: SessionDetachInput
    is_released: Callable[[], bool]
    session_id: str
    owner: asyncio.Task | None = None
    active: bool = True
    receipt: SessionDetachOutput | None = None


_INVOCATION: ContextVar[DetachInvocation | None] = ContextVar("session_detach_invocation", default=None)


def current_detach_invocation() -> DetachInvocation:
    """Require a live dispatch task and a committed association release."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task() or not state.is_released():
        raise RuntimeError("session.detach requires a trusted released association")
    return state


def valid_detach_input(value: object) -> bool:
    """Only original host facts can continue down the notification chain."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and state.is_released() and type(value) is SessionDetachInput and value == state.original
        and valid_surface(value.surface) and type(value.clientId) is str and bool(value.clientId)
        and value.reason in {"detach", "end"}
    )


def valid_detach_output(value: object) -> bool:
    """Only the actual CORE receipt is an accepted notification result."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and state.is_released() and state.receipt is not None and type(value) is SessionDetachOutput
        and type(value.clientId) is str and value == state.receipt
    )


@contextmanager
def bind_detach_invocation(state: DetachInvocation) -> Iterator[None]:
    """Bind one dispatch task without granting a reusable event capability."""
    if not state.active or state.owner is not None:
        raise RuntimeError("session.detach invocation is no longer available")
    state.owner = asyncio.current_task()
    token = _INVOCATION.set(state)
    try:
        yield
    finally:
        state.active = False
        _INVOCATION.reset(token)


def register_session_detach_event(bus: ExtensionEventBus) -> None:
    """Register the type, validators and default notification CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_detach import session_detach_core

    bus.register_event_type(
        SESSION_DETACH_EVENT, match_pattern_subset, session_detach_core,
        input_validator=valid_detach_input, output_validator=valid_detach_output,
    )
