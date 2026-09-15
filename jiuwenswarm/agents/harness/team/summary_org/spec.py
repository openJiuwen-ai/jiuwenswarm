# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixed, host-owned specification for an organization Summary Team."""

from __future__ import annotations

from typing import Any


def _summary_agent_spec(source: Any, *, system_prompt: str) -> Any:
    """Keep only the configured model and language while dropping user-team capabilities."""
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec

    return DeepAgentSpec(
        model=getattr(source, "model", None),
        system_prompt=system_prompt,
        tools=[],
        mcps=[],
        subagents=[],
        rails=None,
        enable_task_loop=True,
        enable_async_subagent=False,
        enable_subagent_runtime=False,
        add_general_purpose_agent=False,
        enable_security_rail=True,
        enable_tool_resilience_rail=True,
        max_iterations=12,
        workspace=None,
        cwd=None,
        project_root=None,
        skills=[],
        enable_skill_discovery=False,
        sys_operation=None,
        enable_sys_operation=False,
        auto_create_workspace=False,
        language=getattr(source, "language", None),
    )


def build_summary_team_spec(
    *, team_id: str, organization_id: str, session_id: str
) -> Any:
    """Build the fixed leader-plus-two-teammate Summary Team spec.

    The host's normal resolver is consulted only for its configured default
    model connection. No user Team members, tools, workspace, AgentGroup, or
    template prompt is copied into the returned specification.
    """
    from openjiuwen.agent_teams.schema.blueprint import (
        LeaderSpec,
        TeamAgentSpec,
        TransportSpec,
    )
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    configured = TeamManager._load_team_spec(session_id)
    leader_source = configured.agents.get("leader")
    teammate_source = configured.agents.get("teammate", leader_source)
    if leader_source is None or teammate_source is None:
        raise ValueError("a configured default model is required for Summary Team")
    leader_prompt = (
        "You are the Summary Team leader. First read only the bound source snapshot, then delegate one "
        "internal analysis task and one drafting task to the two fixed teammates. After both teammates "
        "report, you MUST immediately call org_summary_complete with the final user-facing result. "
        "Internal task completion, team idle, and team pause are not completion of the assigned Summary Task: "
        "do not start another internal task cycle, wait, or poll. A Root Leader message may add delivery "
        "requirements, but it never changes your two-tool protocol. Never create, claim, delegate, review, "
        "or modify organization tasks."
    )
    integrator_prompt = (
        "You are the source integrator. Turn the supplied source snapshot into a structured factual "
        "outline with source attribution. Do not access organization tools or create tasks."
    )
    drafter_prompt = (
        "You are the delivery drafter. Create a user-facing draft only from the supplied source snapshot. "
        "Do not access organization tools or create tasks."
    )
    metadata = {
        "summary_team": True,
        "organization_id": organization_id,
        "capabilities": ["summary"],
    }
    return TeamAgentSpec(
        agents={
            "leader": _summary_agent_spec(leader_source, system_prompt=leader_prompt),
            "source-integrator": _summary_agent_spec(
                teammate_source, system_prompt=integrator_prompt
            ),
            "delivery-drafter": _summary_agent_spec(
                teammate_source, system_prompt=drafter_prompt
            ),
        },
        team_name=team_id,
        lifecycle="persistent",
        enable_team_plan=False,
        teammate_mode="build_mode",
        leader=LeaderSpec(
            member_name="summary-leader",
            display_name="Summary Leader",
            desc="Produces the final organization summary.",
            prompt=leader_prompt,
        ),
        predefined_members=[
            TeamMemberSpec(
                member_name="source-integrator",
                display_name="Source Integrator",
                role_type=TeamRole.TEAMMATE,
                desc="Structures accepted source outputs.",
                prompt=integrator_prompt,
            ),
            TeamMemberSpec(
                member_name="delivery-drafter",
                display_name="Delivery Drafter",
                role_type=TeamRole.TEAMMATE,
                desc="Drafts the user-facing final response.",
                prompt=drafter_prompt,
            ),
        ],
        model_pool=list(getattr(configured, "model_pool", []) or []),
        model_router=getattr(configured, "model_router", None),
        model_intelli_router=getattr(configured, "model_intelli_router", None),
        model_pool_strategy=getattr(configured, "model_pool_strategy", "round_robin"),
        team_mode="predefined",
        dispatch_mode="autonomous",
        # The fixed teammates run in this process.  They still need a real
        # transport so internal task and message events reach their inboxes.
        spawn_mode="inprocess",
        transport=TransportSpec(type="inprocess"),
        metadata=metadata,
    )


__all__ = ["build_summary_team_spec"]
