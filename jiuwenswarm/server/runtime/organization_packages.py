# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared AgentGroup availability and organization-only uninstall cleanup."""

from __future__ import annotations

from typing import Any
import logging

logger = logging.getLogger(__name__)


def _team_package_name(team_id: str, agent_group_name: str = "") -> str:
    if agent_group_name:
        return agent_group_name
    try:
        from jiuwenswarm.server.runtime.team_organization_identity_store import load_organization_team_identity

        return load_organization_team_identity(team_id).get("agent_group_name", "")
    except Exception as exc:
        logger.warning("Cannot resolve organization package identity for %s: %s", team_id, exc)
        return ""


def team_package_availability(team_id: str, agent_group_name: str = "") -> dict[str, Any]:
    from jiuwenswarm.server.runtime.extension_package_manager import resolve_agent_group_dir
    group = _team_package_name(team_id, agent_group_name)
    if not group:
        # Ordinary teams, Summary Teams, and legacy records without reliable
        # package identity must not be disabled based on a display-name guess.
        return {}
    try:
        resolve_agent_group_dir(group)
    except (ValueError, OSError) as exc:
        return {
            "agent_group_name": group, "available": False,
            "unavailable_reason": getattr(exc, "code", "AGENT_GROUP_UNAVAILABLE"),
        }
    except Exception as exc:
        logger.warning("Cannot probe organization package %s for %s: %s", group, team_id, exc)
        return {
            "agent_group_name": group, "available": False,
            "unavailable_reason": "AGENT_GROUP_UNAVAILABLE",
        }
    return {"agent_group_name": group, "available": True}


async def uninstall_organization_agent_group(params: dict[str, Any]) -> None:
    """Uninstall the definition and stop its organization instances, preserving history."""
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.server.runtime import extension_package_manager as packages
    from jiuwenswarm.server.runtime.organization_execution import stopping_teams

    identifier = params.get("id") if params.get("id") not in (None, "") else params.get("name")
    group = packages.resolve_equipment_runtime_id("agent_groups", identifier)
    # No runtime is stopped if filesystem/catalog removal fails.
    packages.uninstall_agent_group(params)
    runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
    if runtime is None:
        return
    by_session: dict[str, set[str]] = {}
    for info in await runtime.pool.list_all_info():
        entry = await runtime.pool.get(info.team_name)
        if entry is None:
            continue
        metadata = getattr(getattr(entry.agent, "spec", None), "metadata", None) or {}
        backend = getattr(entry.agent, "team_backend", None)
        organization = getattr(backend, "org_task_manager", None)
        if not (metadata.get("expert_team") or organization):
            continue
        team_group = _team_package_name(info.team_name, metadata.get("agent_group_name", ""))
        if team_group != group:
            continue
        by_session.setdefault(entry.current_session_id, set()).add(info.team_name)
    for session_id, team_ids in by_session.items():
        async with stopping_teams(session_id, team_ids):
            for team_id in team_ids:
                try:
                    await runtime.stop_team(team_name=team_id, session_id=session_id)
                except Exception as exc:
                    logger.warning(
                        "Package %s was uninstalled, but stopping Team %s in session %s failed: %s",
                        group, team_id, session_id, exc,
                    )
