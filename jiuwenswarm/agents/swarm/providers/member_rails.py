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
from types import SimpleNamespace
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
from openjiuwen.harness.rails import ModelAnomalyDetectionRail

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
from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail
from jiuwenswarm.agents.harness.team.rails.team_skill_storage_policy_rail import (
    TeamSkillStoragePolicyRail,
)
from jiuwenswarm.agents.harness.team.rails.team_skill_library_reload_rail import (
    TeamSkillLibraryReloadRail,
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
# Renamed from ``swarm.team_shared_skill_link_refresh``: the rail behind the
# name reloads Skill views instead of rebuilding a per-team link farm. Team
# specs are rebuilt from the config source on every assembly, so no persisted
# spec carries the old element name.
TEAM_SKILL_LIBRARY_RELOAD = "swarm.team_skill_library_reload"
TEAM_WORKSPACE_REPORT_PATH = "swarm.team_workspace_report_path"
CONTEXT_PROCESSOR = "swarm.context_processor"
MODEL_ANOMALY_DETECTION = "swarm.model_anomaly_detection"
PLUGIN_RAILS = "swarm.plugin_rails"
SKILL_RETRIEVAL_PROMPT = "swarm.skill_retrieval_prompt"
SYMPHONY_ORCHESTRATION_PROMPT = "swarm.symphony_orchestration_prompt"
HEARTBEAT = "swarm.heartbeat"


@harness_element(
    kind=ElementKind.RAIL,
    name=HEARTBEAT,
    description="AgentServer-owned Heartbeat job tools bound to the Team session.",
)
def _build_heartbeat_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> HeartbeatRail | None:
    """Mount the new Job Heartbeat Rail for Team members.

    The service remains process-owned by AgentServer.  The provider only binds
    the member tools to the immutable Team session identity; it never creates a
    scheduler and never uses the deprecated ``RunKind.HEARTBEAT`` rail.
    """
    _ = params
    service = getattr(context, "heartbeat_job_service", None)
    session_id = str(getattr(context, "session_id", "") or "").strip()
    if service is None or not session_id:
        return None
    metadata = dict(getattr(context, "request_metadata", None) or {})
    tool_context = SimpleNamespace(
        channel_id=str(getattr(context, "channel_id", None) or "web"),
        session_id=session_id,
        metadata=metadata,
        user_id=str(getattr(context, "user_id", None) or ""),
        mode=str(getattr(context, "mode", None) or "team"),
        tool_scope=(
            f"team_member_{getattr(context, 'member_card_id', None) or 'unknown'}"
        ),
    )
    return HeartbeatRail(service=service, context=tool_context)


def _workspace_root(ctx: SwarmBuildContext) -> str | None:
    """Resolve the member workspace root path."""
    workspace = getattr(ctx, "workspace", None)
    return getattr(workspace, "root_path", None) if workspace else None


class SkillRetrievalPromptInput(ConstructionInput):
    """Construction inputs for the Skill directory prompt rail."""

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global installed skills source directory.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=SKILL_RETRIEVAL_PROMPT,
    description="Prompt guidance and session reminders for Skill directory retrieval.",
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
    from jiuwenswarm.agents.swarm.providers.tools import (
        skill_retrieval_toolkit_for_context,
    )

    if not is_skill_retrieval_enabled(context.config):
        return None
    SkillRetrievalPromptInput.resolve(params, context)
    toolkit = skill_retrieval_toolkit_for_context(context)
    session_scope = toolkit.session_scope
    return SkillRetrievalPromptRail(
        toolkit=toolkit,
        session_scope=session_scope,
        config_base=context.config,
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
    task_workspace_root: str | None = context_field(
        attr="task_workspace_root",
        description="Shared final-deliverables root for a projectless team "
        "(team-workspace/artifacts/<date>/chat-<n>/). Only set when "
        "project_dir is None; members bound to a project keep it None so "
        "the runtime prompt rail stays on its else branch.",
    )
    team_outputs_dir: str | None = context_field(
        attr="team_outputs_dir",
        description="Final-deliverables directory under task_workspace_root. "
        "Only set when project_dir is None. Surfaced to the team policy rail "
        "as the team info body's deliverables bullet too.",
    )
    trusted_dirs: list[str] | None = context_field(
        attr="trusted_dirs",
        description="Directories the client declared as trusted for this request.",
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
    # per-member work directory under the shared artifact root. Reporting
    # anything else makes the model resolve relative paths against a directory
    # the tools never use.
    #
    # A projectless team member (no project_dir) is handed the shared
    # final-deliverables directory so the runtime prompt rail takes its
    # ``has_projectless_task`` branch — the prompt then names the member's
    # own ``work/<member>/`` as the temporary working directory (cwd moves
    # there, so intermediate files stay isolated per member) and points final
    # deliverables at the shared team ``outputs/``. Members bound to a project
    # keep the task paths unset so the rail's else branch ("deliverables in the
    # project") applies, matching single-agent behaviour.
    #
    # The per-member work directory is resolved through the build context
    # (``SwarmBuildContext.resolve_member_work_dir``) so there is one creation
    # point shared with ``AgentConfigurator``, which sets the same path as the
    # member's shell cwd. Calling the allocator again here would be idempotent
    # but redundant.
    if inp.project_dir or not inp.team_outputs_dir or not inp.task_workspace_root:
        rail.set_runtime_paths(
            cwd=inp.project_dir or inp.member_workspace_root,
            project_dir=inp.project_dir,
            workspace_dir=inp.member_workspace_root,
        )
    else:
        work_dir = context.resolve_member_work_dir()
        # ``task_workspace_root`` is non-None here (guarded above), and the
        # context's resolver returns a path whenever that root is set, so a
        # None would be an internal invariant violation rather than a runtime
        # condition to paper over. Use an explicit raise instead of assert so
        # the guard survives ``python -O`` (G.TES.01).
        if work_dir is None:
            raise RuntimeError(
                "resolve_member_work_dir() returned None despite "
                "task_workspace_root being set"
            )
        rail.set_runtime_paths(
            cwd=work_dir,
            project_dir=inp.project_dir,
            workspace_dir=inp.member_workspace_root,
            task_workspace_root=inp.task_workspace_root,
            task_work_dir=work_dir,
            task_outputs_dir=inp.team_outputs_dir,
        )
    # Without this the member never renders the trusted_dirs policy section a
    # single agent gets from ``_apply_runtime_config_stages``.
    rail.set_trusted_dirs(inp.trusted_dirs)
    return rail


class TeamSkillStoragePolicyInput(ConstructionInput):
    """Construction inputs for the team skill storage policy rail.

    Team-level paths only: the member's own workspace is per-member and the
    team rail tells the member about it as part of its identity.
    """

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="The one physical Skill library every member reads.",
    )
    team_ws_root: str | None = context_field(
        attr="team_ws_root",
        description="Team shared workspace root.",
    )
    team_skill_visibility_path: str | None = context_field(
        attr="team_skill_visibility_path",
        description="Team skills-visibility.json path (declares visibility, "
        "never a Skill source).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SKILL_STORAGE_POLICY,
    description="Team-only policy that stores all skill authoring outputs in "
    "the single shared Skill library.",
    input_model=TeamSkillStoragePolicyInput,
)
def _build_team_skill_storage_policy_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> TeamSkillStoragePolicyRail | None:
    """Build the team skill storage policy rail when the Skill library is known.

    No team skills directory is passed any more: a team owns no Skill
    directory, only a visibility document. Authoring targets the one library,
    and the visibility file is named so the policy can tell the member it is
    not a Skill source.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamSkillStoragePolicyRail`` or ``None`` when no Skill library is
        available.
    """
    inp = TeamSkillStoragePolicyInput.resolve(params, context)
    if not inp.global_skills_dir:
        return None
    return TeamSkillStoragePolicyRail(
        global_skills_dir=inp.global_skills_dir,
        team_workspace_root=inp.team_ws_root,
        team_skill_visibility_file=inp.team_skill_visibility_path,
    )


class TeamSkillLibraryReloadInput(ConstructionInput):
    """Construction inputs for the Skill-library reload rail."""

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="The one physical Skill library (gate; skipped when absent).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SKILL_LIBRARY_RELOAD,
    description="Reload the member's Skill view after a write into the single "
    "physical Skill library.",
    input_model=TeamSkillLibraryReloadInput,
)
def _build_team_skill_library_reload_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> TeamSkillLibraryReloadRail | None:
    """Build the rail that reloads Skill views after a library write.

    Nothing is copied or linked any more, so no session id or team manager is
    needed: the rail reloads the ``SkillUseRail`` instances mounted on its own
    agent, which then re-read the library through the member's visibility
    metadata. No reload hook is injected — the built-in behaviour is exactly
    what is wanted here.

    Args:
        params: Spec params (unused; kept for the provider contract).
        context: Per-member build context.

    Returns:
        A ``TeamSkillLibraryReloadRail`` or ``None`` when no Skill library is
        configured.
    """
    inp = TeamSkillLibraryReloadInput.resolve(params, context)
    if not inp.global_skills_dir:
        return None
    return TeamSkillLibraryReloadRail(global_skills_dir=Path(inp.global_skills_dir))


class TeamWorkspaceReportPathInput(ConstructionInput):
    """Construction inputs for the team workspace report-path rail."""

    team_ws_root: str | None = context_field(
        attr="team_ws_root",
        description="Team shared workspace root path (gate; skipped when absent).",
    )
    project_dir: str | None = context_field(
        attr="project_dir",
        description="User project root for final project deliverables.",
    )
    team_outputs_dir: str | None = context_field(
        attr="team_outputs_dir",
        description="Shared final-deliverables directory for projectless members "
        "(team-workspace/artifacts/<date>/chat-<n>/outputs/). None for members "
        "bound to a project.",
    )
    team_id: str = context_field(attr="team_id", default="", description="Team name.")
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
    rail = TeamWorkspaceReportPathRail(
        root_dir=inp.team_ws_root,
        project_dir=inp.project_dir,
        outputs_dir=inp.team_outputs_dir,
        team_id=inp.team_id,
        language=inp.language,
    )
    rail.bind_swarm_context(context)
    return rail


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


class ModelAnomalyDetectionInput(ConstructionInput):
    """Construction inputs for the model-anomaly / tool-loop compact rail."""

    rail_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="execution_guard.model_anomaly_detection_rail section.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=MODEL_ANOMALY_DETECTION,
    description="Model anomaly detection rail (repeat/stream retry + tool_loop_compact). "
    "Mounted when execution_guard.model_anomaly_detection_rail.enabled is true; "
    "overrides openjiuwen's default ModelAnomalyDetectionRail() so tool_loop_compact "
    "can be enabled from config.",
    input_model=ModelAnomalyDetectionInput,
)
def _build_model_anomaly_detection(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> Any | None:
    """Build openjiuwen ``ModelAnomalyDetectionRail`` from execution_guard config."""
    inp = ModelAnomalyDetectionInput.resolve(params, context)
    rail_cfg = inp.rail_config if isinstance(inp.rail_config, dict) else {}
    if rail_cfg.get("enabled", False) is not True:
        return None
    try:
        return ModelAnomalyDetectionRail(
            max_retries=rail_cfg.get("max_retries", 2),
            repeat_min_pattern_chars=rail_cfg.get("repeat_min_pattern_chars", 2),
            repeat_max_pattern_chars=rail_cfg.get("repeat_max_pattern_chars", 64),
            repeat_min_count=rail_cfg.get("repeat_min_count", 6),
            repeat_min_total_chars=rail_cfg.get("repeat_min_total_chars", 160),
            repeat_window_chars=rail_cfg.get("repeat_window_chars", 1024),
            single_char_repeat_count=rail_cfg.get("single_char_repeat_count", 100),
            tool_loop_compact=rail_cfg.get("tool_loop_compact"),
        )
    except Exception as exc:
        logger.warning("[SwarmRails] ModelAnomalyDetectionRail create failed: %s", exc)
        return None


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
    "TEAM_SKILL_LIBRARY_RELOAD",
    "TEAM_WORKSPACE_REPORT_PATH",
    "CONTEXT_PROCESSOR",
    "MODEL_ANOMALY_DETECTION",
    "PLUGIN_RAILS",
    "SKILL_RETRIEVAL_PROMPT",
    "SYMPHONY_ORCHESTRATION_PROMPT",
    "HEARTBEAT",
    "TEAM_PERMISSION",
    "TEAM_PERMISSION_POLICY",
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
    narrowed_config = (
        narrow_permissions(inp.permissions_config, override)
        if override
        else inp.permissions_config
    )

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
