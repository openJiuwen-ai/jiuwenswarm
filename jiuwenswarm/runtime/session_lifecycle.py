# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral Session lifecycle facts and participant registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from jiuwenswarm.runtime.session.model import (
    ExecutionLifecycleContext,
    SessionLifecycleContext,
)


class SessionKind(str, Enum):
    """Stable business kind used by lifecycle participants."""

    AGENT = "agent"
    TEAM = "team"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDescriptor:
    """Transport-neutral persisted Session identity and routing metadata."""

    session_id: str
    channel_id: str
    mode: str
    work_mode: str
    project_id: str = ""
    project_dir: str = ""
    user_id: str = ""


@dataclass(frozen=True, slots=True)
class SessionLifecycleTarget:
    """Resolved identity shared by activity and permanent-delete events."""

    descriptor: SessionDescriptor
    kind: SessionKind
    team_name: str = ""


@dataclass(frozen=True, slots=True)
class SessionInputIntentEvent:
    """A user intends to continue the Session, independent of any consumer."""

    target: SessionLifecycleTarget
    has_history: bool = False
    view_id: str = ""
    intent_id: str = ""


@dataclass(frozen=True, slots=True)
class SessionForegroundEvent:
    target: SessionLifecycleTarget
    view_id: str
    visible: bool
    has_history: bool = False


@dataclass(frozen=True, slots=True)
class SessionExecutionEvent:
    target: SessionLifecycleTarget
    request_id: str
    has_history: bool = False


@dataclass(frozen=True, slots=True)
class SessionExecutionFinishedEvent:
    target: SessionLifecycleTarget
    request_id: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class SessionInactiveEvent:
    target: SessionLifecycleTarget


class SessionInputIntentDisposition(str, Enum):
    SCHEDULED = "scheduled"
    NOT_NEEDED = "not_needed"


class SessionActivityParticipant(Protocol):
    async def session_input_intent(
        self,
        event: SessionInputIntentEvent,
    ) -> SessionInputIntentDisposition:
        ...

    async def foreground_changed(self, event: SessionForegroundEvent) -> None:
        ...

    async def execution_started(self, event: SessionExecutionEvent) -> None:
        ...

    def execution_finished(self, event: SessionExecutionFinishedEvent) -> None:
        ...

    async def session_inactive(self, event: SessionInactiveEvent) -> None:
        ...


class SessionDeleteParticipant(Protocol):
    @property
    def name(self) -> str:
        ...

    async def before_delete(self, target: SessionLifecycleTarget) -> None:
        ...

    async def release_resources(self, target: SessionLifecycleTarget) -> None:
        ...

    async def delete_failed(
        self,
        target: SessionLifecycleTarget,
        *,
        destructive_started: bool,
    ) -> None:
        ...

    async def delete_committed(self, target: SessionLifecycleTarget) -> None:
        ...


class RuntimeParticipantRegistry:
    """No-fail in-memory participant publication for one AgentRuntime."""

    def __init__(self) -> None:
        self._activity: tuple[SessionActivityParticipant, ...] = ()
        self._delete: tuple[SessionDeleteParticipant, ...] = ()
        self._targets: dict[str, SessionLifecycleTarget] = {}

    def replace_session_participants(
        self,
        *,
        activity: tuple[SessionActivityParticipant, ...],
        delete: tuple[SessionDeleteParticipant, ...],
    ) -> None:
        self._activity = tuple(activity)
        self._delete = tuple(delete)

    def snapshot_activity(self) -> tuple[SessionActivityParticipant, ...]:
        return self._activity

    def snapshot_delete(self) -> tuple[SessionDeleteParticipant, ...]:
        return self._delete

    def remember_target(self, target: SessionLifecycleTarget) -> None:
        self._targets[target.descriptor.session_id] = target

    def target(self, session_id: str) -> SessionLifecycleTarget | None:
        return self._targets.get(session_id)

    def forget_target(self, session_id: str) -> None:
        self._targets.pop(session_id, None)

    def clear_targets(self) -> None:
        self._targets.clear()


class RuntimeResourceLease(Protocol):
    async def release(self) -> None:
        ...


class SessionRuntimeLifecyclePort(Protocol):
    """Define Team-domain lifecycle actions orchestrated by AgentRuntime."""

    async def remove_response_waiter(
        self,
        context: ExecutionLifecycleContext,
        *,
        reason: str,
    ) -> None:
        """Remove the response waiter owned by the current request."""
        ...

    async def pause_session(
        self,
        context: SessionLifecycleContext,
        *,
        reason: str,
        channel_id: str | None = None,
    ) -> None:
        """Pause one Team Session generation."""
        ...

    async def stop_session(
        self,
        context: SessionLifecycleContext,
        *,
        reason: str,
    ) -> None:
        """Stop one Team Session generation."""
        ...

    def is_running(self, context: SessionLifecycleContext) -> bool:
        """Return whether one Team generation owns active work."""
        ...


__all__ = [
    "RuntimeParticipantRegistry",
    "RuntimeResourceLease",
    "SessionRuntimeLifecyclePort",
    "SessionActivityParticipant",
    "SessionDeleteParticipant",
    "SessionDescriptor",
    "SessionExecutionEvent",
    "SessionExecutionFinishedEvent",
    "SessionForegroundEvent",
    "SessionInactiveEvent",
    "SessionInputIntentDisposition",
    "SessionInputIntentEvent",
    "SessionKind",
    "SessionLifecycleTarget",
]
