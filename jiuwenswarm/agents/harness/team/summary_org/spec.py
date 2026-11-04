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
    from openjiuwen.agent_teams.paths import organization_workspace_dir
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
    from openjiuwen.agent_teams.team_workspace.models import TeamWorkspaceConfig
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    configured = TeamManager.load_default_team_spec(session_id)
    summary_dir = organization_workspace_dir(organization_id, session_id) / "summary"
    leader_source = configured.agents.get("leader")
    teammate_source = configured.agents.get("teammate", leader_source)
    if leader_source is None or teammate_source is None:
        raise ValueError("a configured default model is required for Summary Team")
    leader_prompt = (
        "You are the Summary Team leader. First call org_summary_get_inputs and read only its bound source "
        "snapshot. Use root_task and summary_task to identify the requested deliverable, format and acceptance "
        "criteria; facts must come only from bound sources. When a source output already contains the needed "
        "text, use that text. Read a file only when that bound source explicitly cites it and the summary needs "
        "its content. Do not list directories or open files from earlier runs. "
        "For a single short text deliverable, when the user did not ask for code, multiple files, or a long "
        "research document, write the final text yourself from those bound sources, check it once against the "
        "request, and call org_summary_complete. Do not send that short text to source-integrator or "
        "delivery-drafter. "
        "Use source-integrator and delivery-drafter only when the bound sources contain a disagreement that "
        "would change the conclusion, or the user asked for a file, code, or a multi-part specialist "
        "deliverable. Send that assignment once, by an internal Team message. When source-integrator returns "
        "an outline, send it to delivery-drafter with one internal Team message. The outline contains only the "
        "structure the draft needs, source attribution for each part, and disagreements that would change the "
        "conclusion. Do not add research, risk lists, or verification items the user did not request. "
        "Do not also create an internal task for work assigned by message. "
        "Use the Summary Team workspace for intermediate drafts. "
        f"The only directory for a final file is {summary_dir}. Write it there and pass that same absolute "
        "path as result_uri. If there is no final file, put the full text in output_context.description and "
        "omit result_uri. Before calling org_summary_complete, "
        "verify the returned draft covers the requested deliverable, is grounded only in the bound sources, "
        "and has a non-empty concise abstract. If the teammate draft does not, ask delivery-drafter for one "
        "focused revision. "
        "Preserve a disagreement that would change the conclusion rather than inventing a resolution. Existing "
        "accepted artifacts remain the primary deliverable when they satisfy the request; preserve their "
        "locations and types instead of replacing them with a report. After verification, call "
        "org_summary_complete and stop after successful submission. If validation fails, correct the reported "
        "fields and retry without restarting drafting. Do not poll while waiting for internal task events, or "
        "create, claim, delegate, review, or modify organization tasks. "
        "Send the initial drafting instruction once and request at most one focused revision. "
        "If a teammate reports an execution failure, inspect its existing draft instead of waiting for it. "
        "Never treat an error or a draft file alone as completion. A message reply completes a teammate's work "
        "when no internal task is assigned to that teammate. Cancel abandoned internal work rather than "
        "marking it completed."
    )
    integrator_prompt = (
        "You are the source integrator. This overrides the autonomous claim loop: summary-leader assigns this "
        "work by message. Turn the supplied source snapshot into a structured outline with source attribution, "
        "then send the complete outline back to summary-leader by one internal Team message. The outline "
        "contains only the structure the draft needs, source attribution for each part, and disagreements that "
        "would change the conclusion. Do not add research, risk lists, or verification items the user did not "
        "request. When the source text already covers a section, use that text. Read a file only when that "
        "bound source explicitly cites it and the outline needs its content. Do not list directories or open "
        "files from earlier runs. Do not modify Organization source artifacts or create tasks. Requirements "
        "are not additional factual evidence. Call claim_task only when view_task shows an internal task "
        "assigned to you. If no internal task is assigned to you, do not call claim_task. Sending the outline "
        "message completes this work."
    )
    drafter_prompt = (
        "You are the delivery drafter. This overrides the autonomous claim loop: summary-leader assigns this "
        "work by message. Create a user-facing draft only from the supplied source snapshot and outline. "
        "Return the complete draft and a concise abstract to summary-leader by one internal Team message. "
        "Follow the root and Summary Task requirements for language, format and scope. Do not automatically "
        "produce a long report or redo specialist work. Do not add research, risk lists, or verification items "
        "the user did not request. When the source text already covers a section, use that text. Read a file "
        "only when that bound source explicitly cites it and the draft needs its content. Do not list "
        "directories or open files from earlier runs. "
        f"The only directory for a final file is {summary_dir}. When a final file is required, write it there "
        "and report that same absolute path. If an existing artifact must be published as the final file, "
        "copy it unchanged into that directory; source paths can remain in the delivery description. Set "
        "result_type to the actual delivered format. If no final file is required, return the full text in "
        "the message and do not invent a path. Do not modify source-Team artifacts or create tasks. "
        "Perform at most one format check and one focused correction; do not repeatedly generate counting "
        "scripts. Call claim_task only when view_task shows an internal task assigned to you. If no internal "
        "task is assigned to you, do not call claim_task. A file write alone is not delivery."
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
