# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fresh memory Rail/Provider per Team responder; coordinates come from the entry."""
from __future__ import annotations

import logging
from typing import Any

from agent_gateway.common.mode_matrix import is_code_profile_mode
from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import build_jiuwen_memory_rail
from jiuwenswarm.agents.harness.common.rails.memory_forbidden_rail import MemoryForbiddenRail
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.team_memory_binding import (
    resolve_expert_memory_subject,
    uses_jiuwen_team_memory,
)

logger = logging.getLogger(__name__)

TEAM_MEMORY = "swarm.team_memory"


@harness_element(kind=ElementKind.RAIL, name=TEAM_MEMORY,
                 description="Jiuwen Team document memory with host-bound identity and coordinates.")
def build_team_memory(_params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    config = ctx.config or {}
    # Provider selection enables Team memory independently of legacy memory gates.
    if not uses_jiuwen_team_memory(config, ctx.mode):
        return None
    if not ctx.memory_team:
        logger.warning(
            "[TeamMemory] missing host-bound team coordinate; memory rail not created"
        )
        return None
    if ctx.extras.get("team.memory_members_ready") is False:
        return None
    sidebar = "code" if is_code_profile_mode(ctx.mode) else "work"
    member = ctx.member_name or ""
    ref = ctx.extras.get("team.agent_template_ref") or {}
    name = resolve_expert_memory_subject(
        ctx.memory_group, ctx.memory_experts, member, ref, role=getattr(ctx, "role", ""),
    ) or "main_agent"
    rail = build_jiuwen_memory_rail(config,
        scene="team", project=ctx.memory_project, shared_document_root=True,
        team_binding={"team": ctx.memory_team, "member": member, "sidebar": sidebar},
        collective_agent=name)
    if rail is None:
        return None
    # code_agent is part of this responder, not a separate Team member.
    ctx.extras["_coding_memory_rail"] = rail
    return [MemoryForbiddenRail(), rail]
