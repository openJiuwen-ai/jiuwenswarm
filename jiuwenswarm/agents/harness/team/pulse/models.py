# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coordination facts, conflicts, and recovery decisions for TeamPulse.

Facts come from native task and member records. Message text is not a source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Impact(str, Enum):
    """How a conflict affects execution."""

    BLOCKS_EXECUTION = "blocks_execution"
    NEEDS_LEADER = "needs_leader"


class DecisionKind(str, Enum):
    """What a recovery strategy asks the service to do.

    ``APPLY`` releases tasks through the existing reset API.
    ``RECONCILE`` adopts the native row and writes nothing.
    ``ESCALATE`` records the conflict for the leader and writes nothing.
    A future constraint solver returns the same three outcomes.
    """

    APPLY = "apply"
    RECONCILE = "reconcile"
    ESCALATE = "escalate"


# Status sets copied from the SDK's meaning, compared as stored strings so a
# snapshot can be checked without re-reading the database.
ACTIVE_TASK_STATUSES = frozenset({"planning", "in_progress", "in_review"})
EXECUTING_TASK_STATUSES = frozenset({"planning", "in_progress", "in_review", "completed"})
RESETTABLE_TASK_STATUSES = frozenset({"planning", "in_progress", "in_review"})
OWNED_TASK_STATUSES = frozenset(
    {"pending", "blocked", "planning", "in_progress", "in_review"}
)
UNAVAILABLE_MEMBER_STATUSES = frozenset({"shut_down", "shutdown_requested", "error"})

STALE_OWNERSHIP = "stale_ownership"
OWNERSHIP_UNRESOLVED = "ownership_unresolved"
DEPENDENCY_VIOLATION = "dependency_violation"
INVALID_TRANSITION = "invalid_transition"
DUPLICATE_ACTIVE = "duplicate_active"
UNAVAILABLE_OWNER = "unavailable_owner"
PERMISSION_VIOLATION = "permission_violation"


@dataclass(frozen=True, slots=True)
class TaskFact:
    """One task row TeamPulse is allowed to reason about."""

    task_id: str
    status: str
    assignee: str | None = None
    updated_at: int | None = None
    blocked_by: tuple[str, ...] = ()
    summary: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MemberFact:
    """One roster row."""

    member_name: str
    status: str
    role: str = ""


@dataclass(frozen=True, slots=True)
class OwnershipMark:
    """A claim, release, or revoke observed on the monitor stream."""

    kind: str
    member_name: str | None
    event_type: str
    timestamp: int | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """Native fields that justify a conflict."""

    event_type: str | None
    timestamp: int | None
    previous: dict[str, Any]
    current: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Conflict:
    """One violated coordination invariant."""

    conflict_type: str
    task_ids: tuple[str, ...]
    member_names: tuple[str, ...]
    invariant: str
    evidence: Evidence
    impact: Impact


@dataclass(frozen=True, slots=True)
class Decision:
    """Recovery outcome. ``reset_task_ids`` is set only for ``APPLY``."""

    kind: DecisionKind
    reset_task_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A conflict and the decision taken for it."""

    conflict: Conflict
    decision: Decision
    applied: bool


@dataclass
class CoordinationSnapshot:
    """In-memory coordination projection for one team.

    Task and member rows are replaced from native reads. Ownership marks,
    plan requests, and the last observed status survive those refreshes so
    TeamPulse can see transitions and claim races the current row hides.
    """

    team_name: str
    leader_member_name: str | None = None
    tasks: dict[str, TaskFact] = field(default_factory=dict)
    members: dict[str, MemberFact] = field(default_factory=dict)
    previous_status: dict[str, str] = field(default_factory=dict)
    ownership: dict[str, list[OwnershipMark]] = field(default_factory=dict)
    plan_requests: set[str] = field(default_factory=set)
    last_event_type: str | None = None
    last_event_timestamp: int | None = None


def assignee_of(task: TaskFact) -> str | None:
    """Return a stripped assignee, or None when the task is unassigned."""
    name = (task.assignee or "").strip()
    return name or None


def has_text(value: str | None) -> bool:
    """True when a summary or note carries execution evidence."""
    return isinstance(value, str) and bool(value.strip())


def task_row(task: TaskFact) -> dict[str, Any]:
    """JSON-friendly view of a task fact for evidence and fingerprints."""
    return {
        "task_id": task.task_id,
        "status": task.status,
        "assignee": assignee_of(task),
        "updated_at": task.updated_at,
        "blocked_by": sorted(task.blocked_by),
        "summary": task.summary,
        "note": task.note,
    }
