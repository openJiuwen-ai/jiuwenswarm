# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Immutable committed-view facts and validators for session.attach."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, make_dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_surface import (
    RenderSurface, RenderViewport, valid_surface, valid_viewport,
)

SESSION_ATTACH_EVENT = "session.attach"


SessionAttachInput = make_dataclass(
    "SessionAttachInput",
    [
        ("surface", RenderSurface),
        ("clientId", str),
        ("viewport", RenderViewport | None, field(default=None)),
    ],
    namespace={
        "__doc__": "A client's rendering observations, matching Claude Mods field names.",
        "__module__": __name__,
    },
    frozen=True,
    slots=True,
)

SessionAttachOutput = make_dataclass(
    "SessionAttachOutput",
    [("clientId", str)],
    namespace={
        "__doc__": "Actual CORE acknowledgement while this association is still current.",
        "__module__": __name__,
    },
    frozen=True,
    slots=True,
)


@dataclass
class AttachInvocation:
    """Platform-owned validity scope, never supplied through user payloads."""

    original: SessionAttachInput
    is_current: Callable[[], bool]
    validate_target: Callable[[], Awaitable[bool]]
    session_id: str
    owner: asyncio.Task | None = None
    active: bool = True
    receipt: SessionAttachOutput | None = None


_INVOCATION: ContextVar[AttachInvocation | None] = ContextVar("session_attach_invocation", default=None)


def current_attach_invocation() -> AttachInvocation:
    """Require a live host invocation and its still-current association."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task():
        raise RuntimeError("session.attach requires a current trusted association")
    if not state.is_current():
        raise RuntimeError("session.attach requires a current trusted association")
    return state


def valid_attach_input(value: object) -> bool:
    """Only the original committed facts may be forwarded."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task():
        return False
    if not state.is_current():
        return False
    if not isinstance(value, SessionAttachInput) or value.__class__ is not SessionAttachInput:
        return False
    if value != state.original or not valid_surface(value.surface):
        return False
    if not isinstance(value.clientId, str) or value.clientId.__class__ is not str:
        return False
    return bool(value.clientId) and valid_viewport(value.viewport)


def valid_attach_output(value: object) -> bool:
    """Reject fabricated or obsolete acknowledgements from USER handlers."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task():
        return False
    if not state.is_current() or state.receipt is None:
        return False
    if not isinstance(value, SessionAttachOutput) or value.__class__ is not SessionAttachOutput:
        return False
    if not isinstance(value.clientId, str) or value.clientId.__class__ is not str:
        return False
    return value == state.receipt


@contextmanager
def bind_attach_invocation(state: AttachInvocation) -> Iterator[None]:
    """Bind a host scope to one dispatch task, invalidating it on exit."""
    if not state.active or state.owner is not None:
        raise RuntimeError("session.attach invocation is no longer available")
    state.owner = asyncio.current_task()
    token = _INVOCATION.set(state)
    try:
        yield
    finally:
        state.active = False
        _INVOCATION.reset(token)


def register_session_attach_event(bus: ExtensionEventBus) -> None:
    """Register the host type, immutable contract and notification CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_attach import session_attach_core

    bus.register_event_type(
        SESSION_ATTACH_EVENT, match_pattern_subset, session_attach_core,
        input_validator=valid_attach_input, output_validator=valid_attach_output,
    )
