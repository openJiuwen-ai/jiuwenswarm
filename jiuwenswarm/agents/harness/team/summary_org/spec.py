# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixed, host-owned specification for an organization Summary Team."""

from __future__ import annotations

from typing import Any


def _summary_agent_spec(source: Any, *, system_prompt: str) -> Any:
    """Keep model, language and a finite budget while dropping user-team capabilities."""
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, WorkspaceSpec

    budget = getattr(source, "max_iterations", None)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        budget = 200
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
        max_iterations=budget,
        workspace=WorkspaceSpec(stable_base=True),
        cwd=None,
        project_root=None,
        skills=[],
        enable_skill_discovery=False,
        sys_operation=None,
        enable_sys_operation=True,
        auto_create_workspace=True,
        language=getattr(source, "language", None),
    )


def build_summary_team_spec(
    *, team_id: str, organization_id: str, session_id: str, channel_id: str = ""
) -> Any:
    """Build the fixed leader-plus-two-teammate Summary Team spec.

    The host's normal resolver is consulted only for its configured default
    model connection and finite iteration budgets. No user Team members, tools, workspace, AgentGroup, or
    template prompt is copied into the returned specification.
    """
    from openjiuwen.agent_teams.schema.blueprint import (
        LeaderSpec,
        TeamAgentSpec,
        TransportSpec,
    )
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
    from openjiuwen.agent_teams.team_workspace.models import TeamWorkspaceConfig
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    configured = TeamManager.load_default_team_spec(session_id)
    leader_source = configured.agents.get("leader")
    teammate_source = configured.agents.get("teammate", leader_source)
    if leader_source is None or teammate_source is None:
        raise ValueError("a configured default model is required for Summary Team")
    leader_prompt = (
        "You are the Summary Team leader. First call org_summary_get_inputs and read only its bound source "
        "snapshot. Send that snapshot to source-integrator with an internal Team message. When it returns a "
        "structured, source-attributed outline, send the outline to delivery-drafter with an internal Team "
        "message or through the Summary Team workspace. Use the Team workspace for intermediate drafts and "
        "the Organization workspace summary/ directory for final deliverables. Before calling org_summary_complete, "
        "verify the returned draft covers the requested deliverable, is grounded only in the bound sources, "
        "and has a non-empty concise abstract. If it does not, ask delivery-drafter for one focused revision. "
        "After the verification passes, call org_summary_complete exactly once. Do not poll, wait, create, "
        "claim, delegate, review, or modify organization tasks."
        " Send the initial drafting instruction once and request at most one focused revision. "
        "If a teammate reports an execution failure, inspect its existing draft instead of waiting for it. "
        "Never treat an error or a draft file alone as completion. Resolve internal tasks truthfully before "
        "submission; cancel abandoned internal work rather than marking it completed."
    )
    integrator_prompt = (
        "You are the source integrator. Turn the supplied source snapshot into a structured factual "
        "outline with source attribution, then send the complete outline back to summary-leader by internal "
        "Team message or write it to the Summary Team workspace. Do not modify Organization source artifacts "
        "or create tasks."
        " After delivering the outline, complete the assigned internal task with claim_task(status=completed)."
    )
    drafter_prompt = (
        "You are the delivery drafter. Create a user-facing draft only from the supplied source snapshot. "
        "Return the complete draft and a concise abstract to summary-leader by internal Team message or the "
        "Summary Team workspace. Final deliverables belong in the Organization workspace summary/ directory. "
        "Do not modify source-Team artifacts or create tasks."
        " Perform at most one format check and one focused correction; do not repeatedly generate counting "
        "scripts. Send the complete draft, abstract and artifact path to summary-leader, then complete the "
        "assigned internal task with claim_task(status=completed). A file write alone is not delivery."
    )
    metadata = {
        "summary_team": True,
        "organization_id": organization_id,
        "capabilities": ["summary"],
        "channel_id": channel_id,
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
        workspace=TeamWorkspaceConfig(
            enabled=True,
            artifact_dirs=["sources", "outlines", "drafts", "reviews"],
            version_control=True,
        ),
        metadata=metadata,
    )


__all__ = ["build_summary_team_spec"]
