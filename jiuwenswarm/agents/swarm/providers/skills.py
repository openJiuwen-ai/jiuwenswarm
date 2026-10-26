# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-skill rail provider for swarm provider-based team assembly.

This module ports the "member skills" branch of the legacy
``team_manager`` customizer into a config-sourced rail provider. The provider
factory links the member's configured skills into the member workspace
``skills`` directory and returns a ``MemberSkillToolkitRail`` bound to the
shared agent workspace, so members share one skill store while each exposes
only its own configured skill view through directory links.

The directory-preparation helpers below are pure functions extracted from the
former customizer closure: the variables the closure captured implicitly
(``global_skills_dir`` / the per-channel team manager) are now explicit
parameters or resolved from the build context.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)

from jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail import (
    MemberSkillToolkitRail,
)
from jiuwenswarm.agents.harness.team.team_skill_links import (
    is_valid_skill_dir,
    link_skill_dir,
    path_exists_or_link,
    prune_skill_dir_links,
    remove_skill_dir_link,
)
from jiuwenswarm.common.utils import get_agent_workspace_dir, get_agent_skills_dir
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

logger = logging.getLogger(__name__)

# Provider name registered for the member-skill toolkit rail.
MEMBER_SKILL_TOOLKIT = "swarm.member_skill_toolkit"


def _link_member_configured_skills(
    member_skills_dir: Path,
    selected_skills: list[str],
    global_skills_dir: Path,
) -> None:
    """Link the member's configured skills into its own skills directory.

    Synchronizes the member ``skills`` directory so it holds exactly one
    directory link per selected skill, pruning links for skills no longer
    selected. Skills are linked (not copied) so runtime installs/uninstalls in
    the shared store propagate without stale copies.

    Args:
        member_skills_dir: Member workspace ``skills`` directory.
        selected_skills: Skill names selected for this member.
        global_skills_dir: Global agent skills directory to link from.
    """
    if not global_skills_dir.exists():
        logger.warning(
            "[swarm.member_skill_toolkit] global_skills_dir does not exist: %s",
            global_skills_dir,
        )
        return

    selected_skill_set = set(selected_skills)
    member_skills_dir.mkdir(parents=True, exist_ok=True)
    prune_skill_dir_links(global_skills_dir, member_skills_dir, selected_skill_set)
    linked_count = 0
    for skill_dir in global_skills_dir.iterdir():
        if not is_valid_skill_dir(skill_dir):
            continue
        if skill_dir.name not in selected_skill_set:
            continue
        dest = member_skills_dir / skill_dir.name
        if path_exists_or_link(dest):
            continue
        link_skill_dir(skill_dir, dest)
        linked_count += 1
        logger.info(
            "[swarm.member_skill_toolkit] Linked skill '%s' to member workspace",
            skill_dir.name,
        )

    existing_skill_names = {
        path.name for path in member_skills_dir.iterdir() if path_exists_or_link(path)
    }
    missing = sorted(selected_skill_set - existing_skill_names)
    if missing:
        logger.warning(
            "[swarm.member_skill_toolkit] configured skills not found in global dir: %s",
            missing,
        )

    logger.info(
        "[swarm.member_skill_toolkit] Total configured skills linked to member: %d",
        linked_count,
    )


def _extract_skill_name_from_tool_result(result: dict[str, object]) -> str:
    """Extract a skill name from a skill tool result.

    Args:
        result: The skill tool invocation result mapping.

    Returns:
        The resolved skill name, or an empty string when none is present.
    """
    skill = result.get("skill")
    if isinstance(skill, dict):
        skill_name = str(skill.get("name", "")).strip()
        if skill_name:
            return skill_name
    return str(result.get("skill_name", "") or result.get("name", "")).strip()


def _workspace_root(ctx: Any) -> str | None:
    """Resolve the member workspace root path (gate for the toolkit)."""
    workspace = ctx.workspace
    return getattr(workspace, "root_path", None) if workspace else None


class MemberSkillToolkitInput(ConstructionInput):
    """Construction inputs for the member skill-toolkit rail."""

    skills: list[str] = param_field(
        default_factory=list,
        description="Selected member skill names to expose in the toolkit.",
    )
    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root (gate; skipped when absent).",
    )
    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global agent skills directory.",
    )
    session_id: str = context_field(
        attr="session_id",
        default="",
        description="Active session id (for runtime skill-link refresh).",
    )
    channel: str = context_field(
        attr="channel",
        default="default",
        description="Resolved channel key for the per-channel team manager.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=MEMBER_SKILL_TOOLKIT,
    description="Member-scoped skill toolkit rail: prepares the member workspace "
    "skills directory and exposes skill discovery/management.",
    input_model=MemberSkillToolkitInput,
)
def build_member_skill_toolkit(params: dict, ctx: Any) -> object | None:
    """Build a member-scoped skill toolkit rail for the current member.

    Links the member's configured skills into its workspace ``skills``
    directory and returns a ``MemberSkillToolkitRail`` bound to the shared
    agent workspace, wired with a callback that refreshes the link views after
    runtime skill installs/uninstalls.

    Args:
        params: Provider params; ``params["skills"]`` carries the selected
            member skill names (from ``config_specs``).
        ctx: The active ``SwarmBuildContext`` for the current member.

    Returns:
        A ``MemberSkillToolkitRail`` instance, or ``None`` when the member has
        no usable workspace (the capability is skipped for this member).
    """
    inp = MemberSkillToolkitInput.resolve(params, ctx)
    root_path = inp.workspace_root
    if not root_path:
        return None

    member_skills_dir = Path(root_path) / "skills"
    selected_skills = [str(skill).strip() for skill in inp.skills if str(skill).strip()]
    global_skills_dir = Path(inp.global_skills_dir) if inp.global_skills_dir else get_agent_skills_dir()
    agent_workspace_dir = get_agent_workspace_dir()
    session_id = inp.session_id
    channel = inp.channel

    # Link member-configured skills so the member workspace exposes only that
    # member's skill view (no copies, no per-member skills_state.json).
    try:
        member_skills_dir.mkdir(parents=True, exist_ok=True)
        if selected_skills:
            _link_member_configured_skills(
                member_skills_dir, selected_skills, global_skills_dir
            )
    except Exception as exc:
        logger.warning(
            "[swarm.member_skill_toolkit] skill link refresh failed: %s", exc
        )

    # The skill manager / toolkit operate on the shared agent workspace so
    # installs are shared; each member only sees its own linked view.
    member_skill_manager: Any | None = None
    try:
        member_skill_manager = SkillManager(workspace_dir=str(agent_workspace_dir))
    except Exception as exc:
        logger.warning(
            "[swarm.member_skill_toolkit] member SkillManager setup failed: %s", exc
        )

    def refresh_member_skill_links(result: dict[str, object]) -> None:
        """Refresh linked skill views after a member skill tool mutation."""
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

        if result.get("skill_removed") or result.get("removed"):
            skill_name = _extract_skill_name_from_tool_result(result)
            if skill_name:
                remove_skill_dir_link(member_skills_dir / skill_name)
        get_team_manager(channel).refresh_team_shared_skill_links(session_id)

    logger.info(
        "[swarm.member_skill_toolkit] MemberSkillToolkitRail built for skill workspace: %s",
        agent_workspace_dir,
    )
    return MemberSkillToolkitRail(
        workspace_dir=str(agent_workspace_dir),
        manager=member_skill_manager,
        refresh_links=refresh_member_skill_links,
    )


__all__ = [
    "MEMBER_SKILL_TOOLKIT",
    "build_member_skill_toolkit",
]
