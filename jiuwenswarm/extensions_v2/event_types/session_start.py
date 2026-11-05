# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A trusted, package-activation-scoped notification before native input."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from openjiuwen.core.events import (
    EventHandler,
    EventType,
    ExtensionEventBus,
    HandlerId,
    HandlerLevel,
    MatchPattern,
    PackageMetadata,
)

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderSurface, valid_surface

SESSION_START_EVENT = "session.start"
_PACKAGE_PATTERN_KEY = "__session_start_package_id__"


class SessionStartEventBus(ExtensionEventBus):
    """Bind a verified package target when publishing a start handler."""

    def register_event_handler(
        self,
        event_type: EventType,
        handler: EventHandler,
        match_pattern: MatchPattern,
        handler_level: HandlerLevel = HandlerLevel.USER,
        *,
        package_metadata: PackageMetadata | None = None,
    ) -> HandlerId:
        if event_type == SESSION_START_EVENT:
            if not isinstance(package_metadata, PackageMetadata):
                raise ValueError("session.start handler requires verified package metadata")
            if not isinstance(match_pattern, Mapping):
                raise ValueError("session.start match_pattern must be a mapping")
            if _PACKAGE_PATTERN_KEY in match_pattern:
                raise ValueError("session.start package match key is reserved for the host")
            match_pattern = {**match_pattern, _PACKAGE_PATTERN_KEY: package_metadata.package_id}
        return super().register_event_handler(
            event_type, handler, match_pattern, handler_level, package_metadata=package_metadata,
        )


@dataclass(frozen=True, slots=True)
class SessionStartInput:
    """Claude Mods start observations; activation identity stays with the host."""

    cwd: str
    surface: RenderSurface | None
    isInteractive: bool


@dataclass(frozen=True, slots=True)
class SessionStartOutput:
    """The actual cwd echoed by CORE, without changing the running directory."""

    cwd: str


@dataclass
class StartInvocation:
    """Call-local facts owned by the host dispatch boundary."""

    original: SessionStartInput
    session_id: str
    package_id: str
    owner: asyncio.Task | None
    active: bool = True
    receipt: SessionStartOutput | None = None


_INVOCATION: ContextVar[StartInvocation | None] = ContextVar("session_start_invocation", default=None)


def current_start_invocation() -> StartInvocation:
    """Require this task's still-active platform invocation."""
    state = _INVOCATION.get()
    if state is None or not state.active or state.owner is not asyncio.current_task():
        raise RuntimeError("session.start requires a trusted start invocation")
    return state


def valid_start_input(value: object) -> bool:
    """Only the original host input may be forwarded to the next handler."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and isinstance(value, SessionStartInput) and value == state.original
        and isinstance(value.cwd, str) and bool(value.cwd)
        and (Path(value.cwd).is_absolute() or PureWindowsPath(value.cwd).is_absolute())
        and (value.surface is None or valid_surface(value.surface))
        and type(value.isInteractive) is bool
    )


def valid_start_output(value: object) -> bool:
    """A USER result cannot replace the actual CORE notification receipt."""
    state = _INVOCATION.get()
    return (
        state is not None and state.active and state.owner is asyncio.current_task()
        and state.receipt is not None and isinstance(value, SessionStartOutput)
        and value == state.receipt
    )


@contextmanager
def bind_start_invocation(
    value: SessionStartInput, *, session_id: str, package_id: str,
) -> Iterator[StartInvocation]:
    """Bind validation facts for one platform-owned notification scope.

    Args:
        value: Immutable target activation and first-input facts.
        session_id: Trusted identity kept outside the public event payload.
        package_id: Verified activation target kept outside the public event payload.

    Yields:
        Validation state for the platform dispatcher and default CORE receipt.
    """
    state = StartInvocation(value, session_id, package_id, asyncio.current_task())
    token = _INVOCATION.set(state)
    try:
        yield state
    finally:
        state.active = False
        _INVOCATION.reset(token)


def match_session_start(event_input: object, match_pattern: MatchPattern) -> bool:
    """Match the host-bound target first, then the public input fields."""
    state = _INVOCATION.get()
    if (
        state is None or not state.active or state.owner is not asyncio.current_task()
        or match_pattern.get(_PACKAGE_PATTERN_KEY) != state.package_id
    ):
        return False
    public_pattern = {key: value for key, value in match_pattern.items() if key != _PACKAGE_PATTERN_KEY}
    return match_pattern_subset(event_input, public_pattern)


def register_session_start_event(bus: ExtensionEventBus) -> None:
    """Register the host event, validators and its notification CORE."""
    from jiuwenswarm.extensions_v2.event_handlers.session_start import session_start_core

    bus.register_event_type(
        SESSION_START_EVENT, match_session_start, session_start_core,
        input_validator=valid_start_input, output_validator=valid_start_output,
    )
