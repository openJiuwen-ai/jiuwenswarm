# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Widen team task reassignment without editing agent-core.

agent-core only swaps the assignee of an ``in_progress`` task. A task that is
already owned but still ``pending`` or ``blocked`` (assigned, not started)
must be handable the same way: the status stays put, and the row never falls
back through the claimable pool. ``planning`` and ``in_review`` stay excluded
because their artifacts belong to the current owner.
"""
from __future__ import annotations

from typing import Any

from openjiuwen.core.common.logging import team_logger

_PATCHED = False
_REASSIGNABLE_RESTING = frozenset({"pending", "blocked"})


async def _reassign_resting_task(
    dao: Any,
    task_id: str,
    from_assignee: str,
    to_assignee: str,
) -> bool:
    """CAS-swap assignee for an owned task that has not started yet."""
    from sqlalchemy import update

    from openjiuwen.agent_teams.tools.database.engine import get_current_time
    from openjiuwen.agent_teams.tools.database.task_dao import _get_task_model

    team_task_model = _get_task_model()
    now = get_current_time()
    async with dao._sessions.write() as session:
        result = await session.execute(
            update(team_task_model)
            .where(
                team_task_model.task_id == task_id,
                team_task_model.assignee == from_assignee,
                team_task_model.status.in_(list(_REASSIGNABLE_RESTING)),
            )
            .values(assignee=to_assignee, updated_at=now)
        )
        await session.commit()
        swapped = result.rowcount == 1
        if swapped:
            team_logger.info(
                "Task %s reassigned from %s to %s (status preserved)",
                task_id,
                from_assignee,
                to_assignee,
            )
        return swapped


def apply_task_reassign_patch() -> None:
    """Install the wider reassign. Repeat calls do nothing."""
    global _PATCHED
    if _PATCHED:
        return

    from openjiuwen.agent_teams.tools.database.task_dao import TaskDao

    original = TaskDao.reassign_task

    async def reassign_task(self: Any, task_id: str, from_assignee: str, to_assignee: str) -> bool:
        if await original(self, task_id, from_assignee, to_assignee):
            return True
        return await _reassign_resting_task(self, task_id, from_assignee, to_assignee)

    TaskDao.reassign_task = reassign_task  # type: ignore[method-assign]
    _PATCHED = True
    team_logger.info(
        "[TeamReassign] patch applied (pending/blocked tasks keep their status when reassigned)"
    )
