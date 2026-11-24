# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Goal policy values and the scheduled leader setting contract."""

from collections.abc import Mapping
from typing import Any


def normalize_team_goal(raw: object, *, path: str) -> dict[str, Any] | None:
    """Validate a host policy without enabling token budgets."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object or null")
    if set(raw) - {"strategy", "max_attempts"}:
        raise ValueError(f"{path} supports only strategy and max_attempts")
    strategy = raw.get("strategy", "hybrid")
    if strategy not in ("agent_report", "transcript", "hybrid"):
        raise ValueError(f"{path}.strategy must be agent_report, transcript or hybrid")
    attempts = raw.get("max_attempts", 10)
    if attempts is not None and (type(attempts) is not int or attempts < 1):
        raise ValueError(f"{path}.max_attempts must be a positive integer or null")
    return {"strategy": strategy, "max_attempts": attempts}


def leader_goal_enabled(team: Mapping[str, Any]) -> bool:
    """Report effective enablement, including empty policy objects."""
    leader = team.get("leader") or {}
    if not isinstance(leader, Mapping):
        return False
    return team.get("dispatch_mode") == "scheduled" and isinstance(leader.get("goal"), dict)


def resolve_leader_goal_update(
    team: Mapping[str, Any],
    *,
    scheduled: bool | None,
    enabled: bool | None,
) -> tuple[str, dict[str, Any] | None]:
    """Plan both fields before a settings transaction writes anything."""
    mode = team.get("dispatch_mode", "autonomous") if scheduled is None else (
        "scheduled" if scheduled else "autonomous"
    )
    if enabled is True and mode != "scheduled":
        raise ValueError("team_leader_goal_enabled requires scheduled dispatch")
    if mode != "scheduled" or enabled is False:
        return str(mode), None
    leader = team.get("leader") or {}
    if not isinstance(leader, Mapping):
        raise ValueError("modes.team.jiuwen_team.leader must be an object")
    raw = leader.get("goal")
    if enabled is True and raw is None:
        raw = {}
    return str(mode), normalize_team_goal(raw, path="modes.team.jiuwen_team.leader.goal")
