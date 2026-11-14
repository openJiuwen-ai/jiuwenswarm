# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recovery strategies.

``DeterministicRecovery`` is the only strategy in this version. It resets a
task only when the native row shows a single safe correction. Anything that
needs a judgment, including every case a constraint solver might later rank,
is escalated.

``RecoveryStrategy`` is the extension point. A future solver implements
``propose`` and returns the same ``Decision`` values. This module does not
search a constraint space.
"""

from __future__ import annotations

from typing import Protocol

from jiuwenswarm.agents.harness.team.pulse.models import (
    DUPLICATE_ACTIVE,
    PERMISSION_VIOLATION,
    STALE_OWNERSHIP,
    UNAVAILABLE_OWNER,
    Conflict,
    CoordinationSnapshot,
    Decision,
    DecisionKind,
    TaskFact,
    assignee_of,
    has_text,
)


class RecoveryStrategy(Protocol):
    """Choose a coordination decision from a conflict and the native snapshot."""

    def propose(self, conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
        """Return apply, reconcile, or escalate. Must not mutate the snapshot."""


class DeterministicRecovery:
    """Explainable rules backed by native task rows."""

    @staticmethod
    def propose(conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
        if conflict.conflict_type == STALE_OWNERSHIP:
            return Decision(
                kind=DecisionKind.RECONCILE,
                reason="Native assignee matches one claimant; the other claim is stale.",
            )
        if conflict.conflict_type == DUPLICATE_ACTIVE:
            return _duplicate_decision(conflict, snapshot)
        if conflict.conflict_type == UNAVAILABLE_OWNER:
            return _unavailable_decision(conflict, snapshot)
        if conflict.conflict_type == PERMISSION_VIOLATION:
            return _permission_decision(conflict, snapshot)
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="No unambiguous coordination correction is available.",
        )


def _duplicate_decision(conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
    tasks = _tasks(conflict, snapshot)
    if len(tasks) < 2:
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="Duplicate-active conflict no longer matches two native tasks.",
        )
    evidenced = [task for task in tasks if _has_execution_evidence(task)]
    if len(evidenced) > 1:
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="More than one active task has execution evidence.",
        )
    if len(evidenced) == 1:
        keeper = evidenced[0]
    else:
        keeper = min(tasks, key=_keep_order)
    reset_ids = tuple(
        task.task_id
        for task in tasks
        if task.task_id != keeper.task_id and _blank_resettable(task)
    )
    if not reset_ids:
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="The extra active tasks are not safe to release.",
        )
    return Decision(
        kind=DecisionKind.APPLY,
        reset_task_ids=reset_ids,
        reason=f"Keep {keeper.task_id} and release the other active tasks.",
    )


def _unavailable_decision(conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
    tasks = _tasks(conflict, snapshot)
    if len(tasks) != 1:
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="Unavailable-owner conflict does not name one task.",
        )
    task = tasks[0]
    if (
        task.status == "planning"
        and _blank(task)
        and task.task_id not in snapshot.plan_requests
    ):
        return Decision(
            kind=DecisionKind.APPLY,
            reset_task_ids=(task.task_id,),
            reason="Departed member holds an unstarted planning task with no plan request.",
        )
    return Decision(
        kind=DecisionKind.ESCALATE,
        reason="Departed member holds work that reset cannot safely release.",
    )


def _permission_decision(conflict: Conflict, snapshot: CoordinationSnapshot) -> Decision:
    tasks = _tasks(conflict, snapshot)
    if len(tasks) != 1:
        return Decision(
            kind=DecisionKind.ESCALATE,
            reason="Permission conflict does not name one task.",
        )
    task = tasks[0]
    if _blank_resettable(task):
        return Decision(
            kind=DecisionKind.APPLY,
            reset_task_ids=(task.task_id,),
            reason="Assignee is the leader or is missing from the roster, and the task has no execution evidence.",
        )
    return Decision(
        kind=DecisionKind.ESCALATE,
        reason="Invalid assignee holds work that is not safe to release.",
    )


def _tasks(conflict: Conflict, snapshot: CoordinationSnapshot) -> list[TaskFact]:
    return [snapshot.tasks[task_id] for task_id in conflict.task_ids if task_id in snapshot.tasks]


def _has_execution_evidence(task: TaskFact) -> bool:
    return task.status == "in_review" or not _blank(task)


def _blank(task: TaskFact) -> bool:
    return not has_text(task.summary) and not has_text(task.note)


def _blank_resettable(task: TaskFact) -> bool:
    """Planning or in-progress with no summary or note. In-review is evidence."""
    return task.status in {"planning", "in_progress"} and _blank(task)


def _keep_order(task: TaskFact) -> tuple:
    """Earliest updated_at wins. Missing timestamps sort after known ones."""
    missing = task.updated_at is None
    return (missing, task.updated_at or 0, task.task_id, assignee_of(task) or "")
