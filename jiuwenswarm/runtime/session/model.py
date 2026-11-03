# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Models shared by the Session runtime coordinator and its ports."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

TEAM_EXECUTION_CONTEXT_METADATA_KEY = "_team_execution_context"

# ``chat.send`` requests carrying this truthy param attach a response
# subscription to an already-running Team Session without submitting input.
from jiuwenswarm.common.schema.message import TEAM_ATTACH_ONLY_PARAM  # noqa: F401,E402  re-export


class NoControlTargetError(RuntimeError):
    """No execution of the Session can take an answer.

    None waits on the interactions it names, or the same answer is already
    being delivered.
    """


class AnswerInFlightError(NoControlTargetError):
    """The same answer is already being delivered; that delivery settles what it answers."""


class SessionExecutionEndedError(RuntimeError):
    """Control input completed after the execution that asked for it was gone.

    Deliberately not an ``asyncio.CancelledError``: the delivering task was
    never cancelled, and reporting cancellation would make upstream stream
    handlers treat a real, user-visible failure as a silent abort.
    """


class SessionCloseTimeoutError(RuntimeError):
    """A Session still owns tasks and cannot release its runtime resources."""

    def __init__(self, session_id: str, execution_ids: tuple[str, ...]) -> None:
        self.session_id = session_id
        self.execution_ids = execution_ids
        super().__init__(
            f"session {session_id} still has running executions: "
            f"{', '.join(execution_ids)}"
        )


class RuntimeSessionState(str, Enum):
    READY = "ready"
    ACTIVE = "active"
    QUIESCING = "quiescing"
    CLOSED = "closed"


class SessionPersistencePolicy(str, Enum):
    PERSISTENT = "persistent"


class SessionWorkKind(str, Enum):
    CHAT_UNARY = "chat_unary"
    CHAT_STREAM = "chat_stream"
    SESSION_MESSAGE = "session_message"
    GOAL_STREAM = "goal_stream"
    GOAL_CONTROL = "goal_control"
    GOAL_ATTACH = "goal_attach"
    CONTROL_INPUT = "control_input"
    SESSION_INPUT = "session_input"
    HEARTBEAT = "heartbeat"
    TEAM_REQUEST = "team_request"
    TEAM_CONTROL = "team_control"
    TEAM_BACKGROUND = "team_background"

    @property
    def scheduled(self) -> bool:
        return self in {
            SessionWorkKind.CHAT_UNARY,
            SessionWorkKind.CHAT_STREAM,
            SessionWorkKind.SESSION_MESSAGE,
        }

    @property
    def latest_first(self) -> bool:
        return self in {
            SessionWorkKind.CHAT_UNARY,
            SessionWorkKind.CHAT_STREAM,
        }


class SessionExecutionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_CONTROL = "waiting_for_control"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            SessionExecutionState.SUCCEEDED,
            SessionExecutionState.FAILED,
            SessionExecutionState.CANCELLED,
        }


class ExecutionCancelCause(str, Enum):
    """Describe why an execution or Session lifecycle is ending."""

    CONSUMER_CLOSED = "consumer_closed"
    CLIENT_DISCONNECTED = "client_disconnected"
    USER_PAUSE = "user_pause"
    USER_CANCEL = "user_cancel"
    SESSION_DELETED = "session_deleted"
    RUNTIME_CLOSED = "runtime_closed"


class TeamWaiterKind(str, Enum):
    """Describe the response waiter owned by one Team execution."""

    NONE = "none"
    PERSISTENT = "persistent"
    REQUEST = "request"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionLifecycleContext:
    """Identify one in-process generation of a product Session."""

    session_id: str
    generation: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionLifecycleContext:
    """Carry the transport-neutral identity of one Runtime execution."""

    session: SessionLifecycleContext
    execution_id: str
    request_id: str
    work_kind: SessionWorkKind
    channel_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamExecutionDetails:
    """Describe Team-specific ownership attached to one Runtime execution."""

    control_id: str | None = None
    background_trigger: str | None = None
    session_message_id: str | None = None
    waiter_kind: TeamWaiterKind = TeamWaiterKind.NONE
    # A pure subscription survives business pause and observes later rounds.
    observation_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamExecutionContext(ExecutionLifecycleContext):
    """Carry Team-specific request ownership without coupling the coordinator."""

    details: TeamExecutionDetails


@dataclass(slots=True)
class SessionExecutionHandle:
    execution_id: str
    session_id: str
    request_id: str
    generation: int
    work_kind: SessionWorkKind
    channel_id: str = "default"
    team: TeamExecutionDetails | None = None
    parent_execution_id: str | None = None
    root_work_kind: SessionWorkKind | None = None
    root_request_id: str | None = None
    # The interactions (questions, approvals, ...) this execution's pause emitted;
    # an answer to any of them resumes it.
    waiting_control_ids: frozenset[str] = frozenset()
    state: SessionExecutionState = SessionExecutionState.QUEUED
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    cancellation_requested: bool = False
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    agent: Any = field(default=None, repr=False)
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    retain_owner_task: bool = field(default=False, repr=False)

    def snapshot(self) -> SessionExecutionSnapshot:
        return SessionExecutionSnapshot(
            execution_id=self.execution_id,
            session_id=self.session_id,
            request_id=self.request_id,
            generation=self.generation,
            work_kind=self.work_kind,
            channel_id=self.channel_id,
            team=self.team,
            parent_execution_id=self.parent_execution_id,
            root_work_kind=self.root_work_kind,
            root_request_id=self.root_request_id,
            waiting_control_ids=self.waiting_control_ids,
            state=self.state,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
            cancellation_requested=self.cancellation_requested,
        )


@dataclass(frozen=True, slots=True)
class SessionExecutionSnapshot:
    execution_id: str
    session_id: str
    request_id: str
    generation: int
    work_kind: SessionWorkKind
    channel_id: str
    team: TeamExecutionDetails | None
    parent_execution_id: str | None
    root_work_kind: SessionWorkKind | None
    root_request_id: str | None
    waiting_control_ids: frozenset[str]
    state: SessionExecutionState
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class RuntimeSessionSnapshot:
    session_id: str
    channel_id: str
    persistence_policy: SessionPersistencePolicy
    generation: int
    state: RuntimeSessionState
    executions: tuple[SessionExecutionSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelExecutionResult:
    matched: int
    cancelled: int
    timed_out: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CloseSessionResult:
    session_id: str
    generation: int | None
    existed: bool
    timed_out: tuple[str, ...] = ()
