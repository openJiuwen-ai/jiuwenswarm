"""Read-only, round-scoped private task boards for organization cards."""

import asyncio
import logging
from typing import Any

from openjiuwen.agent_teams.context import reset_session_id, set_session_id

from jiuwenswarm.server.runtime.group_service import (
    group_organization_teams, update_group_organization_plan,
)

logger = logging.getLogger(__name__)


async def capture_task_baselines(session_id: str, plan: dict, organization_id: str, task_pool: Any) -> None:
    """Exclude old instance work before this organization root can create work."""
    if "private_task_baselines" in plan:
        return
    reader = getattr(getattr(getattr(task_pool, "db", None), "task", None), "get_team_tasks", None)
    if not callable(reader):
        return
    baselines = {}
    team_ids = list(dict.fromkeys(
        str(member.get("team_id")) for member in group_organization_teams(session_id, organization_id)
        if member.get("team_id")
    ))
    # SummaryTeamLauncher reserves this organization-specific identity. Capture
    # it before root creation too, even though its board is normally empty.
    team_ids.append(f"org-summary-{organization_id}")

    async def capture(team_id: str) -> None:
        try:
            tasks = await asyncio.wait_for(reader(team_id), timeout=2)
            baselines[team_id] = [str(task.task_id) for task in tasks]
        except Exception:
            # Unknown history must never be interpreted as an empty board.
            baselines[team_id] = None
            logger.warning("Organization task baseline unavailable team=%s", team_id, exc_info=True)

    token = set_session_id(session_id)
    try:
        await asyncio.gather(*(capture(team_id) for team_id in team_ids))
    finally:
        reset_session_id(token)
    plan["private_task_baselines"] = baselines
    update_group_organization_plan(session_id, str(plan["plan_id"]), private_task_baselines=baselines)


async def add_expert_task_details(
    session_id: str, plan: dict, task_pool: Any, organization_tasks: list,
    details: dict, previous: dict, *, fail_unfinished: bool = False,
) -> None:
    """Project each mentioned team's current-round board once, below its first row.

    Private tasks have no organization-child foreign key. The disclosure is
    explicitly the team's round-wide board, not an inferred per-child binding.
    A baseline prevents tasks from earlier uses of the same instance leaking in.
    """
    baselines = plan.get("private_task_baselines", {})
    reader = getattr(getattr(getattr(task_pool, "db", None), "task", None), "get_team_tasks", None)
    seen = set()
    token = set_session_id(session_id)
    try:
        for task in organization_tasks:
            row_id = str(task.task_id)
            if row_id not in details:
                continue
            assignment = getattr(task, "assignment", None)
            team_id = str(getattr(task, "assigned_team_id", None)
                          or getattr(assignment, "team_id", "") or "")
            if not team_id or team_id in seen:
                continue
            # Only teams captured for this organization may contribute rows.
            if team_id not in baselines:
                continue
            seen.add(team_id)
            detail = details[row_id]
            old = previous.get(row_id, {})
            detail["expert_tasks"] = old.get("expert_tasks", [])
            detail["expert_tasks_unavailable"] = False
            excluded = baselines[team_id]
            if excluded is None or not callable(reader):
                detail["expert_tasks_unavailable"] = True
                continue
            try:
                tasks = await asyncio.wait_for(reader(team_id), timeout=2)
                # Retain observed tasks if the team later cleans its board.
                projected = {item["task_id"]: dict(item) for item in detail["expert_tasks"]}
                for private in tasks:
                    if str(private.task_id) in excluded:
                        continue
                    status = str(getattr(private.status, "value", private.status)).lower()
                    if fail_unfinished and status not in {"completed", "cancelled", "failed"}:
                        status = "interrupted"
                    projected[str(private.task_id)] = {
                        "task_id": str(private.task_id),
                        "title": str(private.title or private.task_id),
                        "assignee": str(private.assignee or ""),
                        "status": status,
                    }
                if fail_unfinished:
                    for item in projected.values():
                        if item["status"] not in {"completed", "cancelled", "failed"}:
                            item["status"] = "interrupted"
                detail["expert_tasks"] = sorted(projected.values(), key=lambda item: item["task_id"])
            except Exception:
                detail["expert_tasks_unavailable"] = True
                logger.warning("Organization private task refresh failed team=%s", team_id, exc_info=True)
    finally:
        reset_session_id(token)
