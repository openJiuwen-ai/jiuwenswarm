# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm member rail providers (config-sourced, per-member).

Each provider is a factory ``factory(params, context) -> rail | list | None``
invoked by openjiuwen at build time with the per-member ``SwarmBuildContext``.
Returning ``None`` / ``[]`` means "skip this rail for this member" (config gate).
Providers take precedence over same-named class registrations.

Mirrors the legacy ``build_member_rails`` runtime-prompt / report-path /
context-processor segments and the team manager plugin-rails segment, but driven
by the build context instead of imperatively threaded dataclasses.
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
from openjiuwen.agent_teams.rails.team_context import (
    get_messager,
    get_permissions_override,
    get_team_backend,
)

from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
    RuntimePromptRail,
)
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
    SkillRetrievalPromptRail,
)
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyOrchestrationRail,
)
from jiuwenswarm.agents.harness.team.rails.team_deliverable_location_rail import (
    TeamDeliverableLocationRail,
)
from jiuwenswarm.agents.harness.team.rails.team_skill_storage_policy_rail import (
    TeamSkillStoragePolicyRail,
)
from jiuwenswarm.agents.harness.team.rails.team_shared_skill_link_refresh_rail import (
    TeamSharedSkillLinkRefreshRail,
)
from jiuwenswarm.agents.harness.team.rails.team_workspace_report_path_rail import (
    TeamWorkspaceReportPathRail,
)
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    _build_context_processor_rail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

RUNTIME_PROMPT = "swarm.runtime_prompt"
TEAM_SKILL_STORAGE_POLICY = "swarm.team_skill_storage_policy"
TEAM_SHARED_SKILL_LINK_REFRESH = "swarm.team_shared_skill_link_refresh"
TEAM_WORKSPACE_REPORT_PATH = "swarm.team_workspace_report_path"
TEAM_DELIVERABLE_LOCATION = "swarm.team_deliverable_location"
CONTEXT_PROCESSOR = "swarm.context_processor"
PLUGIN_RAILS = "swarm.plugin_rails"
SKILL_RETRIEVAL_PROMPT = "swarm.skill_retrieval_prompt"
SYMPHONY_ORCHESTRATION_PROMPT = "swarm.symphony_orchestration_prompt"
TEAM_PERMISSION_POLICY = "swarm.team_permission_policy"
TEAM_MEMBER_IDENTITY = "swarm.team_member_identity"


def _workspace_root(ctx: SwarmBuildContext) -> str | None:
    """Resolve the member workspace root path."""
    workspace = getattr(ctx, "workspace", None)
    return getattr(workspace, "root_path", None) if workspace else None


class SkillRetrievalPromptInput(ConstructionInput):
    """Construction inputs for the agentic skill retrieval prompt rail."""

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global installed skills source directory.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=SKILL_RETRIEVAL_PROMPT,
    description="Lightweight prompt guidance for agentic installed-skill tree retrieval.",
    input_model=SkillRetrievalPromptInput,
)
def _build_skill_retrieval_prompt_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> SkillRetrievalPromptRail | None:
    """Build the skill retrieval prompt rail when the feature is enabled."""
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
        is_skill_retrieval_enabled,
    )
    from jiuwenswarm.agents.swarm.providers.tools import visible_skill_names_for_list_skill
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    if not is_skill_retrieval_enabled():
        return None
    SkillRetrievalPromptInput.resolve(params, context)
    manager = SkillManager()
    return SkillRetrievalPromptRail(
        manager=manager,
        visible_skill_names=lambda: visible_skill_names_for_list_skill(context),
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=SYMPHONY_ORCHESTRATION_PROMPT,
    description="Leader-only prompt guidance for Symphony orchestration.",
)
def _build_symphony_orchestration_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> SymphonyOrchestrationRail | None:
    """Build the Symphony orchestration prompt rail for the team leader."""
    _ = params
    if getattr(context, "role", "") != "leader":
        return None
    return SymphonyOrchestrationRail()


class RuntimePromptInput(ConstructionInput):
    """Construction inputs for the member runtime prompt rail."""

    language: str = context_field(
        attr="language",
        default="cn",
        description="Resolved member language code.",
    )
    channel: str = context_field(
        attr="channel",
        default="default",
        description="Resolved channel key.",
    )
    project_dir: str | None = context_field(
        attr="project_dir",
        description="Resolved user project directory (seeds the TUI cwd policy).",
    )
    member_workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Current member workspace root (cwd fallback without a project).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=RUNTIME_PROMPT,
    description="Per-member runtime prompt rail bound to the member's language and channel.",
    input_model=RuntimePromptInput,
)
def _build_runtime_prompt_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> RuntimePromptRail:
    """Build the runtime prompt rail for a member.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``RuntimePromptRail`` bound to the member's language and channel.
    """
    inp = RuntimePromptInput.resolve(params, context)
    rail = RuntimePromptRail(language=inp.language, channel=inp.channel)
    # Team members use their own RuntimePromptRail instance. Bind it to the
    # originating request so runtime state is read from the active session
    # instead of the process-wide ``default`` state file.
    rail.set_mode(context.mode)
    rail.set_session_id(context.session_id)
    # Report the member's real working directory. cwd and workspace are
    # separate layers (see openjiuwen.core.sys_operation.cwd): with a project
    # the member runs in the project dir, without one it runs in its own
    # workspace. Reporting anything else makes the model resolve relative
    # paths against a directory the tools never use.
    rail.set_runtime_paths(
        cwd=inp.project_dir or inp.member_workspace_root,
        project_dir=inp.project_dir,
        workspace_dir=inp.member_workspace_root,
    )
    return rail


class TeamSkillStoragePolicyInput(ConstructionInput):
    """Construction inputs for the team skill storage policy rail.

    Team-level paths only: the member's own workspace is per-member and the
    team rail tells the member about it as part of its identity.
    """

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global shared skills source directory.",
    )
    team_ws_root: str | None = context_field(
        attr="team_ws_root",
        description="Team shared workspace root.",
    )
    team_skills_dir: str | None = context_field(
        attr="team_skills_dir",
        description="Team shared skills linked view.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SKILL_STORAGE_POLICY,
    description="Team-only policy that stores all skill authoring outputs in "
    "the global shared skills source directory.",
    input_model=TeamSkillStoragePolicyInput,
)
def _build_team_skill_storage_policy_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> TeamSkillStoragePolicyRail | None:
    """Build the team skill storage policy rail when the global skill root exists.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamSkillStoragePolicyRail`` or ``None`` when no global skills
        directory is available.
    """
    inp = TeamSkillStoragePolicyInput.resolve(params, context)
    if not inp.global_skills_dir:
        return None
    return TeamSkillStoragePolicyRail(
        global_skills_dir=inp.global_skills_dir,
        team_workspace_root=inp.team_ws_root,
        team_skills_dir=inp.team_skills_dir,
    )


class TeamSharedSkillLinkRefreshInput(ConstructionInput):
    """Construction inputs for refreshing team shared skill links."""

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global shared skills source directory.",
    )
    session_id: str = context_field(
        attr="session_id",
        default="",
        description="Active session id.",
    )
    channel: str = context_field(
        attr="channel",
        default="default",
        description="Resolved channel key for the per-channel team manager.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SHARED_SKILL_LINK_REFRESH,
    description="Refresh team shared skill links after tools write into the "
    "global shared skills source directory.",
    input_model=TeamSharedSkillLinkRefreshInput,
)
def _build_team_shared_skill_link_refresh_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> TeamSharedSkillLinkRefreshRail | None:
    """Build the rail that refreshes team shared skill links after writes.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamSharedSkillLinkRefreshRail`` or ``None`` when required runtime
        context is missing.
    """
    inp = TeamSharedSkillLinkRefreshInput.resolve(params, context)
    if not inp.global_skills_dir or not inp.session_id:
        return None

    def refresh_links() -> None:
        """Refresh the current team's shared skill link view."""
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

        get_team_manager(inp.channel).refresh_team_shared_skill_links(inp.session_id)

    return TeamSharedSkillLinkRefreshRail(
        global_skills_dir=Path(inp.global_skills_dir),
        refresh_links=refresh_links,
    )


class TeamWorkspaceReportPathInput(ConstructionInput):
    """Construction inputs for the team workspace report-path rail."""

    team_ws_root: str | None = context_field(
        attr="team_ws_root",
        description="Team shared workspace root path (gate; skipped when absent).",
    )
    team_id: str = context_field(attr="team_id", default="", description="Team name.")
    project_dir: str | None = context_field(
        attr="project_dir",
        description="Resolved user project directory (deliverable path examples).",
    )
    language: str = context_field(
        attr="language",
        default="cn",
        description="Resolved member language code.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_WORKSPACE_REPORT_PATH,
    description="Rewrites report paths under the shared team workspace root "
    "(skipped when no shared root is configured).",
    input_model=TeamWorkspaceReportPathInput,
)
def _build_team_workspace_report_path_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> TeamWorkspaceReportPathRail | None:
    """Build the team workspace report-path rail when a shared root exists.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamWorkspaceReportPathRail`` rooted at the team workspace, or
        ``None`` when no shared workspace root is configured.
    """
    inp = TeamWorkspaceReportPathInput.resolve(params, context)
    if not inp.team_ws_root:
        return None
    return TeamWorkspaceReportPathRail(
        root_dir=inp.team_ws_root,
        team_id=inp.team_id,
        language=inp.language,
        project_dir=inp.project_dir,
    )


class TeamDeliverableLocationInput(ConstructionInput):
    """Construction inputs for the team deliverable location rail."""

    project_dir: str | None = context_field(
        attr="project_dir",
        description="Resolved user project directory (gate; skipped when absent).",
    )
    member_workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Current member workspace root (contrasted in the prompt).",
    )
    language: str = context_field(
        attr="language",
        default="cn",
        description="Resolved member language code.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_DELIVERABLE_LOCATION,
    description="Guides team members to write user-facing deliverables into "
                "the task working directory instead of the private workspace (skipped "
                "when the session has no project directory).",
    input_model=TeamDeliverableLocationInput,
)
def _build_team_deliverable_location_rail(
        params: dict[str, Any],
        context: SwarmBuildContext,
) -> TeamDeliverableLocationRail | None:
    """Build the deliverable location rail when a project directory exists.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamDeliverableLocationRail`` or ``None`` when the session has no
        resolved project directory (behavior stays unchanged there).
    """
    inp = TeamDeliverableLocationInput.resolve(params, context)
    if not inp.project_dir:
        return None
    return TeamDeliverableLocationRail(
        project_dir=inp.project_dir,
        member_workspace_root=inp.member_workspace_root,
        language=inp.language,
    )


class TeamMemberIdentityInput(ConstructionInput):
    """Construction inputs for the team member identity rail.

    全部经 param_field 传入(随 TeamAgentSpec 序列化,冷恢复/分布式重建不丢);
    非专家团(装配期未渲染身份文本)identity_text 为空,provider 返回 None 不挂载。
    """

    role: str = param_field(
        default="",
        description="Member role (leader / teammate),决定身份块称谓(主理人/成员)。",
    )
    display_name: str = param_field(
        default="",
        description="Member display name(团包模板 agent_card.name)。",
    )
    group_display: str = param_field(
        default="",
        description="专家团显示名(团包 manifest)。",
    )
    identity_text: dict[str, str] = param_field(
        default_factory=dict,
        description="装配期渲染好的身份块({cn, en} 双份);空则不挂载。",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_MEMBER_IDENTITY,
    description="Injects the member's expert-group identity as a standalone "
                "PromptSection (P:10); skipped when no identity text was rendered.",
    input_model=TeamMemberIdentityInput,
)
def _build_team_member_identity_rail(
        params: dict[str, Any],
        context: SwarmBuildContext,
) -> Any:
    """Build the team member identity rail when identity text was rendered.

    专家团主理人/成员身份注入(design/team/08):团包 persona 拍平进 Team 层
    prompt 后落在 team_extra(P:17)杂项槽位;本 rail 把身份块提到独立
    section(P:10),与单专家 identity section 锚定同构。

    Returns:
        A ``TeamMemberIdentityRail``, or ``None`` for non-expert-group teams.
    """
    from jiuwenswarm.agents.harness.team.rails.team_member_identity_rail import (
        TeamMemberIdentityRail,
    )

    inp = TeamMemberIdentityInput.resolve(params, context)
    if not inp.identity_text:
        return None
    return TeamMemberIdentityRail(
        role=inp.role,
        display_name=inp.display_name,
        group_display=inp.group_display,
        identity_text=inp.identity_text,
    )


class ContextProcessorInput(ConstructionInput):
    """Construction inputs for the context-compression rail."""

    context_engine_enabled: bool = param_field(
        default=True,
        description="Whether the context engine is enabled in config (gate).",
    )
    context_engine_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Context-engine config (compressor sub-configs).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CONTEXT_PROCESSOR,
    description="Context-compression rail, mounted only when the context engine "
    "is enabled in config.",
    input_model=ContextProcessorInput,
)
def _build_context_processor(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> Any | None:
    """Build the context-compression rail when the context engine is enabled.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A preset ``ContextProcessorRail`` when enabled, otherwise ``None``.
    """
    inp = ContextProcessorInput.resolve(params, context)
    if not inp.context_engine_enabled:
        return None
    return _build_context_processor_rail(
        {"context_engine_config": inp.context_engine_config}
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=PLUGIN_RAILS,
    description="User-registered extension rails: a fresh instance of every "
    "registered rail extension, one per member.",
)
def _build_plugin_rails(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> list[Any]:
    """Build user-registered extension rails for a member.

    Enumerates every registered rail extension and instantiates a fresh
    instance per member, skipping any that fail to load.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A list of extension rail instances (possibly empty).
    """
    rail_manager = get_rail_manager()
    rails: list[Any] = []
    for rail_name in rail_manager.get_registered_rail_names():
        try:
            rail_instance = rail_manager.load_rail_instance_without_enabled_check(
                rail_name,
            )
            if rail_instance is not None:
                rails.append(rail_instance)
        except Exception as exc:
            logger.warning(
                "[SwarmRails] load extension rail %s failed: %s",
                rail_name,
                exc,
            )
    return rails


__all__ = [
    "RUNTIME_PROMPT",
    "TEAM_SKILL_STORAGE_POLICY",
    "TEAM_SHARED_SKILL_LINK_REFRESH",
    "TEAM_WORKSPACE_REPORT_PATH",
    "CONTEXT_PROCESSOR",
    "PLUGIN_RAILS",
    "SKILL_RETRIEVAL_PROMPT",
    "SYMPHONY_ORCHESTRATION_PROMPT",
    "TEAM_DELIVERABLE_LOCATION",
    "TEAM_PERMISSION",
    "TEAM_PERMISSION_POLICY",
    "TEAM_MEMBER_IDENTITY",
]


# ---------------------------------------------------------------------------
# team.permission_policy — TeamPermissionPolicyRail (leader prompt section)
# ---------------------------------------------------------------------------


TEAM_PERMISSION_POLICY = "swarm.team_permission_policy"


class TeamPermissionPolicyInput(ConstructionInput):
    """Construction inputs for the team permission policy prompt rail."""

    permissions_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Permission config dict used to generate permission "
        "rule descriptions via format_base_permissions_for_desc.",
    )
    language: str = context_field(
        attr="language",
        default="cn",
        description="Resolved member language code.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_PERMISSION_POLICY,
    description="Injects teammate permission rules into the leader's system prompt.",
    input_model=TeamPermissionPolicyInput,
)
def _build_team_permission_policy_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> Any | None:
    """Build the permission policy prompt rail for the leader."""
    inp = TeamPermissionPolicyInput.resolve(params, context)
    if not inp.permissions_config.get("enabled"):
        return None

    from jiuwenswarm.agents.harness.team.rails.team_permission_policy_rail import (
        TeamPermissionPolicyRail,
    )

    return TeamPermissionPolicyRail(
        permissions_config=inp.permissions_config,
        language=inp.language,
    )


# ---------------------------------------------------------------------------
# team.permission — TeamPermissionRail (swarm-side thin provider)
# ---------------------------------------------------------------------------


TEAM_PERMISSION = "swarm.team_permission"


class TeamPermissionInput(ConstructionInput):
    """Construction inputs for the team permission rail."""

    permissions_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Full permission config dict (as consumed by "
        "openjiuwen.harness.security.engine.PermissionEngine).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_PERMISSION,
    description="Team-mode permission guardrail with leader-mediated ASK resolution.",
    input_model=TeamPermissionInput,
)
def _build_team_permission_rail(params: dict[str, Any], context: Any) -> Any | None:
    """Build the team permission rail (gated on backend + messager + permissions enabled).

    Thin swarm provider: reads ``permissions_config`` from ``RailSpec.params``
    (baked by config_specs) and runtime handles from ``BuildContext.extras``
    (injected by AgentConfigurator). The actual permission logic —
    openjiuwen.harness.security.engine.PermissionEngine,
    openjiuwen.agent_teams.rails.team_permission_rail.TeamPermissionRail,
    openjiuwen.agent_teams.rails.team_permission_rail.TeamApprovalOrchestrator —
    lives in openjiuwen.
    """
    backend = get_team_backend(context)
    messager = get_messager(context)
    if backend is None or messager is None:
        return None

    inp = TeamPermissionInput.resolve(params, context)
    if not inp.permissions_config.get("enabled"):
        return None

    from openjiuwen.agent_teams.rails.team_permission_rail import (
        TeamApprovalOrchestrator,
        TeamPermissionRail,
    )
    from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
    from openjiuwen.harness.security.host import ToolPermissionHost
    from openjiuwen.agent_teams.security.narrowing import narrow_permissions

    override = get_permissions_override(context)
    narrowed_config = narrow_permissions(inp.permissions_config, override) if override else inp.permissions_config

    message_manager = TeamMessageManager(
        backend.team_name,
        backend.member_name,
        backend.db,
        messager,
    )
    orchestrator = TeamApprovalOrchestrator(
        message_manager=message_manager,
        leader_member_name=backend.leader_member_name,
    )

    host = ToolPermissionHost(
        request_permission_confirmation=orchestrator.handle_approval_request,
    )

    return TeamPermissionRail(
        config=narrowed_config,
        host=host,
    )
