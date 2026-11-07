# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure coordination-invariant checks.

These report conflicts. They do not decide whether a reset is safe.
"""

from __future__ import annotations

from openjiuwen.agent_teams.schema.status import TASK_TRANSITIONS, TaskStatus, is_valid_transition

from jiuwenswarm.agents.harness.team.pulse.models import (
    ACTIVE_TASK_STATUSES,
    DEPENDENCY_VIOLATION,
    DUPLICATE_ACTIVE,
    EXECUTING_TASK_STATUSES,
    INVALID_TRANSITION,
    OWNERSHIP_UNRESOLVED,
    OWNED_TASK_STATUSES,
    PERMISSION_VIOLATION,
    STALE_OWNERSHIP,
    UNAVAILABLE_MEMBER_STATUSES,
    UNAVAILABLE_OWNER,
    Conflict,
    CoordinationSnapshot,
    Evidence,
    Impact,
    TaskFact,
    assignee_of,
    task_row,
)
from jiuwenswarm.agents.harness.team.pulse.snapshot import open_claimants

_INVARIANT_OWNERSHIP = (
    "A task has one assignee. A later claim without release or revoke is stale "
    "only when the native assignee is one of the claimants."
)
_INVARIANT_OWNERSHIP_UNRESOLVED = "A task assignee must match a recorded claim."
_INVARIANT_DEPENDENCY = "A task does not execute while blocked_by is non-empty."
_INVARIANT_TRANSITION = "Task status changes must follow TASK_TRANSITIONS."
_INVARIANT_CAPACITY = "A member has at most one active task."
_INVARIANT_AVAILABILITY = (
    "A shut-down, shutting-down, or errored member does not keep owned work."
)
_INVARIANT_PERMISSION = "A task assignee must be a non-leader roster member."


def detect_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    """Return every current invariant violation, in a stable order."""
    conflicts: list[Conflict] = []
    conflicts.extend(_ownership_conflicts(snapshot))
    conflicts.extend(_dependency_conflicts(snapshot))
    conflicts.extend(_transition_conflicts(snapshot))
    conflicts.extend(_duplicate_conflicts(snapshot))
    conflicts.extend(_unavailable_conflicts(snapshot))
    conflicts.extend(_permission_conflicts(snapshot))
    return conflicts


def _evidence(snapshot: CoordinationSnapshot, *, previous: dict, current: dict) -> Evidence:
    return Evidence(
        event_type=snapshot.last_event_type,
        timestamp=snapshot.last_event_timestamp,
        previous=previous,
        current=current,
    )


def _ownership_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    found: list[Conflict] = []
    for task_id, marks in snapshot.ownership.items():
        task = snapshot.tasks.get(task_id)
        if task is None:
            continue
        claimants = open_claimants(marks)
        if len(claimants) < 2:
            continue
        assignee = assignee_of(task)
        current = {
            "assignee": assignee,
            "claimants": list(claimants),
            "status": task.status,
        }
        previous = {"claimants": list(claimants)}
        if assignee in claimants:
            conflict_type = STALE_OWNERSHIP
            invariant = _INVARIANT_OWNERSHIP
            impact = Impact.BLOCKS_EXECUTION
        else:
            conflict_type = OWNERSHIP_UNRESOLVED
            invariant = _INVARIANT_OWNERSHIP_UNRESOLVED
            impact = Impact.NEEDS_LEADER
        found.append(
            Conflict(
                conflict_type=conflict_type,
                task_ids=(task_id,),
                member_names=tuple(claimants),
                invariant=invariant,
                evidence=_evidence(snapshot, previous=previous, current=current),
                impact=impact,
            )
        )
    return found


def _dependency_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    found: list[Conflict] = []
    for task in _tasks_in_order(snapshot):
        if task.status not in EXECUTING_TASK_STATUSES or not task.blocked_by:
            continue
        found.append(
            Conflict(
                conflict_type=DEPENDENCY_VIOLATION,
                task_ids=(task.task_id,),
                member_names=_member_names(task),
                invariant=_INVARIANT_DEPENDENCY,
                evidence=_evidence(
                    snapshot,
                    previous={},
                    current=task_row(task),
                ),
                impact=Impact.BLOCKS_EXECUTION,
            )
        )
    return found


def _transition_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    found: list[Conflict] = []
    for task in _tasks_in_order(snapshot):
        previous = snapshot.previous_status.get(task.task_id)
        if previous is None or previous == task.status:
            continue
        if _transition_allowed(previous, task.status):
            continue
        found.append(
            Conflict(
                conflict_type=INVALID_TRANSITION,
                task_ids=(task.task_id,),
                member_names=_member_names(task),
                invariant=_INVARIANT_TRANSITION,
                evidence=_evidence(
                    snapshot,
                    previous={"status": previous},
                    current={"status": task.status, "task_id": task.task_id},
                ),
                impact=Impact.BLOCKS_EXECUTION,
            )
        )
    return found


def _duplicate_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    grouped: dict[str, list[TaskFact]] = {}
    for task in snapshot.tasks.values():
        if task.status not in ACTIVE_TASK_STATUSES:
            continue
        owner = assignee_of(task)
        if owner is None:
            continue
        grouped.setdefault(owner, []).append(task)
    found: list[Conflict] = []
    for owner in sorted(grouped):
        active = sorted(grouped[owner], key=lambda item: item.task_id)
        if len(active) < 2:
            continue
        found.append(
            Conflict(
                conflict_type=DUPLICATE_ACTIVE,
                task_ids=tuple(task.task_id for task in active),
                member_names=(owner,),
                invariant=_INVARIANT_CAPACITY,
                evidence=_evidence(
                    snapshot,
                    previous={},
                    current={"assignee": owner, "tasks": [task_row(task) for task in active]},
                ),
                impact=Impact.BLOCKS_EXECUTION,
            )
        )
    return found


def _unavailable_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    found: list[Conflict] = []
    for task in _tasks_in_order(snapshot):
        if task.status not in OWNED_TASK_STATUSES:
            continue
        owner = assignee_of(task)
        if owner is None or owner == snapshot.leader_member_name:
            continue
        member = snapshot.members.get(owner)
        if member is None or member.status not in UNAVAILABLE_MEMBER_STATUSES:
            continue
        found.append(
            Conflict(
                conflict_type=UNAVAILABLE_OWNER,
                task_ids=(task.task_id,),
                member_names=(owner,),
                invariant=_INVARIANT_AVAILABILITY,
                evidence=_evidence(
                    snapshot,
                    previous={},
                    current={
                        "task": task_row(task),
                        "member_status": member.status,
                        "plan_requested": task.task_id in snapshot.plan_requests,
                    },
                ),
                impact=Impact.NEEDS_LEADER,
            )
        )
    return found


def _permission_conflicts(snapshot: CoordinationSnapshot) -> list[Conflict]:
    found: list[Conflict] = []
    leader = snapshot.leader_member_name
    for task in _tasks_in_order(snapshot):
        if task.status not in OWNED_TASK_STATUSES:
            continue
        owner = assignee_of(task)
        if owner is None:
            continue
        unknown = owner not in snapshot.members
        is_leader = leader is not None and owner == leader
        if not unknown and not is_leader:
            continue
        found.append(
            Conflict(
                conflict_type=PERMISSION_VIOLATION,
                task_ids=(task.task_id,),
                member_names=(owner,),
                invariant=_INVARIANT_PERMISSION,
                evidence=_evidence(
                    snapshot,
                    previous={},
                    current={
                        "task": task_row(task),
                        "leader_member_name": leader,
                        "on_roster": not unknown,
                    },
                ),
                impact=Impact.NEEDS_LEADER,
            )
        )
    return found


def _tasks_in_order(snapshot: CoordinationSnapshot) -> list[TaskFact]:
    return [snapshot.tasks[task_id] for task_id in sorted(snapshot.tasks)]


def _member_names(task: TaskFact) -> tuple[str, ...]:
    owner = assignee_of(task)
    if owner is None:
        return ()
    return (owner,)


def _transition_allowed(previous: str, current: str) -> bool:
    try:
        previous_status = TaskStatus(previous)
        current_status = TaskStatus(current)
    except ValueError:
        return False
    return is_valid_transition(previous_status, current_status, TASK_TRANSITIONS)
