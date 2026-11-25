# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settle the task board when a team runtime is stopped.

``TeamRuntimeManager.stop_team`` tears down coordination and drops the pool
entry but leaves non-terminal task rows (``in_progress`` / ``blocked`` / …)
unchanged so cold-recover can reuse them. On the enterprise chat surface that
leaves the right-hand board spinning after interrupt or refresh: history reads
the stale ``in_progress`` row with no live runtime behind it.

Wrap ``stop_team`` so every stop path cancels active tasks first (core already
exposes ``TaskManager.cancel_all_tasks`` and publishes ``TaskCancelledEvent``).
Settle failures are logged and do not block the real stop.
"""
from __future__ import annotations

from typing import Any

from openjiuwen.core.common.logging import team_logger

_PATCHED = False


async def _settle_tasks_before_stop(
    manager: Any,
    *,
    team_name: str,
    session_id: str,
) -> None:
    """Cancel non-terminal tasks for the pooled team, if any."""
    resolve = getattr(manager, "_resolve_entry", None)
    if not callable(resolve):
        return
    entry = await resolve(team_name=team_name, session_id=session_id)
    if entry is None:
        return
    agent = getattr(entry, "agent", None)
    task_manager = getattr(agent, "task_manager", None) if agent is not None else None
    cancel_all = getattr(task_manager, "cancel_all_tasks", None)
    if not callable(cancel_all):
        return

    from openjiuwen.agent_teams.context import reset_session_id, set_session_id

    token = set_session_id(session_id)
    try:
        cancelled = await cancel_all()
    finally:
        reset_session_id(token)

    count = len(cancelled) if cancelled is not None else 0
    team_logger.info(
        "[TeamStopSettle] cancelled %s task(s) before stop_team team=%s session=%s",
        count,
        team_name,
        session_id,
    )


def apply_stop_team_settle_patch() -> None:
    """Install the stop-time task settle. Repeat calls do nothing."""
    global _PATCHED
    if _PATCHED:
        return

    from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

    original = TeamRuntimeManager.stop_team

    async def stop_team(self: Any, *, team_name: str, session_id: str) -> bool:
        try:
            await _settle_tasks_before_stop(
                self, team_name=team_name, session_id=session_id
            )
        except Exception:
            team_logger.exception(
                "[TeamStopSettle] settle failed; continuing stop_team team=%s session=%s",
                team_name,
                session_id,
            )
        return await original(self, team_name=team_name, session_id=session_id)

    TeamRuntimeManager.stop_team = stop_team  # type: ignore[method-assign]
    _PATCHED = True
    team_logger.info(
        "[TeamStopSettle] patch applied (cancel active tasks before stop_team)"
    )
