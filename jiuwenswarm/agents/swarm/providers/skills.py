# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-skill visibility and toolkit provider for swarm team assembly.

Skills live in exactly one physical library (``get_agent_skills_dir()``).
Which Skill a member may see is *metadata* — a ``skills-visibility.json``
document at the member workspace root — not a materialized per-member
``skills/`` directory of symlinks. This module owns three things:

* turning a build context into a live visibility provider that re-reads the
  documents on every call (:func:`build_member_skill_visibility_provider`),
* the one-shot ``(enabled, disabled)`` snapshot for the Skill rails and Skill
  listings that can only take name sets
  (:func:`compose_member_skill_visibility`), and
* the ``MemberSkillToolkitRail`` that exposes skill discovery / install /
  uninstall to the member.

It deliberately does **not** seed the document. Seeding has exactly one owner,
``openjiuwen.agent_teams.rails.team_skill_use_rail.create_team_skill_use_rail``,
which every member reaches through the ``core.team.skill_use`` rail declared in
``config_specs`` and which already carries the same
``config.agents.<role>.skills`` value as its ``bootstrap_allow``. A second
writer here would contend for the same file lock on every build and its skip
rules would drift from the owner's.

The document is the authority: configuration seeds it once and never
overwrites it afterwards, so an authorization change made at runtime survives
a config edit. An empty ``allow`` list means "inherit the whole library".
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
)
from openjiuwen.agent_teams.skill import (
    FileSkillVisibilityProvider,
    build_skill_visibility_provider,
)

from jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail import (
    MemberSkillToolkitRail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.utils import get_agent_workspace_dir, get_agent_skills_dir
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

logger = logging.getLogger(__name__)

# Provider name registered for the member-skill toolkit rail.
MEMBER_SKILL_TOOLKIT = "swarm.member_skill_toolkit"


def build_member_skill_visibility_provider(
    ctx: SwarmBuildContext,
) -> FileSkillVisibilityProvider | None:
    """Build the live Skill visibility provider for the member in *ctx*.

    The returned object re-reads the member and team metadata files on every
    call, so an authorization change made by another process is picked up
    without rebuilding anything. It is what a Skill rail able to take a
    visibility provider should be handed; callers that can only take plain
    name sets go through :func:`compose_member_skill_visibility` instead.

    Args:
        ctx: The per-member build context.

    Returns:
        A provider, or ``None`` when the context carries neither a member
        workspace nor a (team, member) identity — the caller should then fall
        back to the process-wide disabled list alone.
    """
    member_path = ctx.resolve_member_skill_visibility_path()
    team_path = ctx.team_skill_visibility_path
    member_name = str(ctx.member_name or "").strip()
    if not member_path:
        return None
    return build_skill_visibility_provider(
        member_path=member_path,
        member_id=member_name or ctx.team_id,
        team_path=team_path,
        team_id=ctx.team_id,
        global_disabled_loader=_load_global_disabled_skills,
    )


def compose_member_skill_visibility(
    ctx: SwarmBuildContext,
) -> tuple[set[str], set[str]]:
    """Resolve the member's ``(enabled, disabled)`` Skill name sets once.

    Snapshot form of :func:`build_member_skill_visibility_provider`, for the
    callers that can only accept plain name sets. An empty ``enabled`` set means
    "inherit the whole library", never "deny everything"; ``disabled`` always
    wins over ``enabled``. Both rules are the ones ``SkillUseRail`` applies to
    its own ``enabled_skills`` / ``disabled_skills``, which is what keeps a
    listing and what the member may actually invoke in agreement.

    Args:
        ctx: The per-member build context.

    Returns:
        The composed ``(enabled, disabled)`` sets. Without a resolvable member
        identity only the process-wide disabled list applies.
    """
    provider = build_member_skill_visibility_provider(ctx)
    if provider is None:
        return set(), set(_load_global_disabled_skills())
    return provider()


def _load_global_disabled_skills() -> list[str]:
    """Return the process-wide execution-disabled Skill names.

    Imported lazily so the swarm provider modules do not drag the skill runtime
    package in at import time.
    """
    from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

    return load_execution_disabled_skills()


def _workspace_root(ctx: Any) -> str | None:
    """Resolve the member workspace root path (gate for the toolkit)."""
    workspace = ctx.workspace
    return getattr(workspace, "root_path", None) if workspace else None


def _member_visibility_path(ctx: Any) -> str | None:
    """Resolve the member's Skill visibility metadata path from the context.

    The resolver is tolerant of a context that predates the method (a stale
    build context rebuilt from an older seed), but never of a value that is not
    callable: the path is derived per member and a non-callable attribute of
    that name would be a different concept, not a usable path.
    """
    resolve = getattr(ctx, "resolve_member_skill_visibility_path", None)
    return resolve() if callable(resolve) else None


class MemberSkillToolkitInput(ConstructionInput):
    """Construction inputs for the member skill-toolkit rail.

    No seed allow-list is taken: the member's visibility document is seeded by
    the ``core.team.skill_use`` rail alone (see the module docstring), which
    already receives ``config.agents.<role>.skills`` as its ``bootstrap_allow``.
    """

    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root (gate; skipped when absent).",
    )
    visibility_path: str | None = context_field(
        resolver=_member_visibility_path,
        description="Member skills-visibility.json path (reported, not written).",
    )
    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="The one physical Skill library.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=MEMBER_SKILL_TOOLKIT,
    description="Member-scoped skill toolkit rail: exposes skill discovery "
    "and management over the single shared Skill library.",
    input_model=MemberSkillToolkitInput,
)
def build_member_skill_toolkit(params: dict, ctx: Any) -> object | None:
    """Build a member-scoped skill toolkit rail for the current member.

    Returns a ``MemberSkillToolkitRail`` bound to the shared agent workspace.
    It writes no visibility metadata: the member's ``skills-visibility.json``
    has a single seeder, the team Skill rail, and the path is resolved here
    only to name the governing document in the build log.

    No ``on_skill_library_changed`` hook is injected. An install writes into the
    one physical library and deliberately does not extend any allow-list: a
    member with an empty allow-list already inherits the whole library, and a
    member with an explicit one must be granted the new Skill on purpose. The
    rail's own view reload is all that a library mutation needs.

    Args:
        params: Provider params (none consumed; kept for the provider contract).
        ctx: The active ``SwarmBuildContext`` for the current member.

    Returns:
        A ``MemberSkillToolkitRail`` instance, or ``None`` when the member has
        no usable workspace (the capability is skipped for this member).
    """
    inp = MemberSkillToolkitInput.resolve(params, ctx)
    root_path = inp.workspace_root
    if not root_path:
        return None

    visibility_path = inp.visibility_path

    # The skill manager / toolkit operate on the shared agent workspace so
    # installs land in the one library every member reads.
    agent_workspace_dir = get_agent_workspace_dir()
    member_skill_manager: Any | None = None
    try:
        member_skill_manager = SkillManager(workspace_dir=str(agent_workspace_dir))
    except Exception as exc:
        logger.warning(
            "[swarm.member_skill_toolkit] member SkillManager setup failed: %s", exc
        )

    logger.info(
        "[swarm.member_skill_toolkit] MemberSkillToolkitRail built "
        "(library=%s, visibility=%s)",
        inp.global_skills_dir or str(get_agent_skills_dir()),
        visibility_path,
    )
    return MemberSkillToolkitRail(
        workspace_dir=str(agent_workspace_dir),
        manager=member_skill_manager,
    )


__all__ = [
    "MEMBER_SKILL_TOOLKIT",
    "build_member_skill_visibility_provider",
    "build_member_skill_toolkit",
    "compose_member_skill_visibility",
]
