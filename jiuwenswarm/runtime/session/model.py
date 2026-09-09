"""Models shared by the Session runtime coordinator and its ports."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
    GOAL_STREAM = "goal_stream"
    GOAL_CONTROL = "goal_control"
    GOAL_ATTACH = "goal_attach"
    CONTROL_INPUT = "control_input"

    @property
    def scheduled(self) -> bool:
        return self in {
            SessionWorkKind.CHAT_UNARY,
            SessionWorkKind.CHAT_STREAM,
        }


class SessionExecutionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_CONTROL = "waiting_for_control"
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


@dataclass(slots=True)
class SessionExecutionHandle:
    execution_id: str
    session_id: str
    request_id: str
    generation: int
    work_kind: SessionWorkKind
    parent_execution_id: str | None = None
    waiting_control_id: str | None = None
    state: SessionExecutionState = SessionExecutionState.QUEUED
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    cancellation_requested: bool = False
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def snapshot(self) -> SessionExecutionSnapshot:
        return SessionExecutionSnapshot(
            execution_id=self.execution_id,
            session_id=self.session_id,
            request_id=self.request_id,
            generation=self.generation,
            work_kind=self.work_kind,
            parent_execution_id=self.parent_execution_id,
            waiting_control_id=self.waiting_control_id,
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
    parent_execution_id: str | None
    waiting_control_id: str | None
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
