# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fold monitor events into a coordination snapshot.

The snapshot stores ownership and plan-request history from the event
stream. Task and member fields are installed from native reads, not from
event payloads, so a lost race cannot overwrite the database row.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.monitor.models import MonitorEventType

from jiuwenswarm.agents.harness.team.pulse.models import (
    CoordinationSnapshot,
    MemberFact,
    OwnershipMark,
    TaskFact,
)

_CLAIM_EVENTS = frozenset({
    MonitorEventType.TASK_CLAIMED,
    MonitorEventType.TASK_STARTED,
})
_RELEASE_EVENTS = frozenset({MonitorEventType.TASK_RELEASED})
_REVOKE_EVENTS = frozenset({MonitorEventType.TASK_REVOKED})

COORDINATION_EVENT_TYPES = frozenset({
    MonitorEventType.TASK_CREATED,
    MonitorEventType.TASK_PLAN_REQUEST,
    MonitorEventType.TASK_PLAN_RESPONSE,
    MonitorEventType.TASK_UPDATED,
    MonitorEventType.TASK_CLAIMED,
    MonitorEventType.TASK_STARTED,
    MonitorEventType.TASK_COMPLETED,
    MonitorEventType.TASK_CANCELLED,
    MonitorEventType.TASK_UNBLOCKED,
    MonitorEventType.TASK_RELEASED,
    MonitorEventType.TASK_REVOKED,
    MonitorEventType.TASK_SUBMITTED_FOR_REVIEW,
    MonitorEventType.TASK_VERIFIED,
    MonitorEventType.TASK_REVISION_REQUESTED,
    MonitorEventType.MEMBER_SPAWNED,
    MonitorEventType.MEMBER_RESTARTED,
    MonitorEventType.MEMBER_STATUS_CHANGED,
    MonitorEventType.MEMBER_SHUTDOWN,
    MonitorEventType.MEMBER_CANCELED,
})


def is_coordination_event(event: Any) -> bool:
    """True for task and member events. Message text is ignored."""
    return getattr(event, "event_type", None) in COORDINATION_EVENT_TYPES


def install_native_rows(
    snapshot: CoordinationSnapshot,
    *,
    leader_member_name: str | None,
    tasks: list[TaskFact],
    members: list[MemberFact],
) -> None:
    """Replace task and member rows. Event history is left in place."""
    snapshot.leader_member_name = (leader_member_name or "").strip() or None
    snapshot.tasks = {task.task_id: task for task in tasks}
    snapshot.members = {member.member_name: member for member in members}


def fold_monitor_event(snapshot: CoordinationSnapshot, event: Any) -> None:
    """Record ownership and plan-request facts from one monitor event."""
    event_type = getattr(event, "event_type", None)
    if event_type not in COORDINATION_EVENT_TYPES:
        return
    type_value = getattr(event_type, "value", str(event_type))
    timestamp = getattr(event, "timestamp", None)

    task_id = getattr(event, "task_id", None)
    if not isinstance(task_id, str) or not task_id:
        return
    snapshot.last_event_type = str(type_value)
    snapshot.last_event_timestamp = timestamp if isinstance(timestamp, int) else None
    member_name = getattr(event, "member_name", None)
    member = member_name.strip() if isinstance(member_name, str) and member_name.strip() else None

    if event_type == MonitorEventType.TASK_PLAN_REQUEST:
        snapshot.plan_requests.add(task_id)
        return
    if event_type in _CLAIM_EVENTS:
        if member is None:
            return
        snapshot.ownership.setdefault(task_id, []).append(
            OwnershipMark(
                kind="claim",
                member_name=member,
                event_type=str(type_value),
                timestamp=snapshot.last_event_timestamp,
            )
        )
        return
    if event_type in _RELEASE_EVENTS or event_type in _REVOKE_EVENTS:
        kind = "revoke" if event_type in _REVOKE_EVENTS else "release"
        snapshot.ownership.setdefault(task_id, []).append(
            OwnershipMark(
                kind=kind,
                member_name=member,
                event_type=str(type_value),
                timestamp=snapshot.last_event_timestamp,
            )
        )


def open_claimants(marks: list[OwnershipMark]) -> list[str]:
    """Claimants since the last release or revoke, in arrival order.

    A second claim without a release or revoke leaves both names. A repeated
    claim by the same member does not.
    """
    holders: list[str] = []
    for mark in marks:
        if mark.kind in {"release", "revoke"}:
            holders.clear()
            continue
        if mark.kind == "claim" and mark.member_name and mark.member_name not in holders:
            holders.append(mark.member_name)
    return holders


def adopt_recorded_assignee(snapshot: CoordinationSnapshot, task_id: str) -> None:
    """Collapse ownership history to the native assignee. No task write."""
    task = snapshot.tasks.get(task_id)
    assignee = (task.assignee or "").strip() if task is not None and task.assignee else ""
    if assignee:
        snapshot.ownership[task_id] = [
            OwnershipMark(
                kind="claim",
                member_name=assignee,
                event_type="reconciled",
                timestamp=snapshot.last_event_timestamp,
            )
        ]
        return
    snapshot.ownership[task_id] = []
