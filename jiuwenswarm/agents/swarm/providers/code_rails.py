# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-sourced code-mode rail providers for swarm team assembly.

Each code (code.team / team.plan) member rail is declared as a ``swarm.code_*``
factory resolved during ``spec.build()``. The factories mirror the construction
in ``interface_code.py::_build_*_rail`` but read everything from the build
context (config / project_dir / workspace) instead of a live adapter, so code
members are assembled **purely declaratively** — no ``configure_team_member_agent``,
no post-build / rail.init imperative mounting.

The ``CodingMemoryRail`` instance is stashed on ``ctx.extras`` so the code_agent
sub-agent can share it: ``DeepAgentSpec.build`` builds rails before sub-agents
under the same ``build_ctx`` (whose ``extras`` is one shared dict). See
:mod:`jiuwenswarm.agents.swarm.providers.code_subagents`.

Heavy / adapter-coupled symbols (``create_coding_memory_rail``,
``JiuwenAgentModeRail``, ``CodeAgentRail``) are imported lazily inside the
factories to avoid pulling the code adapter at module load.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
CODE_RUNTIME_PROMPT = "swarm.code_runtime_prompt"
CODE_PROJECT_MEMORY = "swarm.code_project_memory"
PERMISSION_INTERRUPT = "swarm.permission_interrupt"
CODE_CODING_MEMORY = "swarm.code_coding_memory"
CODE_AGENT_MODE = "swarm.code_agent_mode"
TEAM_PLAN_APPROVAL = "swarm.team_plan_approval"
STRUCTURED_ASK_USER = "swarm.structured_ask_user"
CODE_TASK_PLANNING = "swarm.code_task_planning"
CODE_AGENT_RAIL = "swarm.code_agent_rail"
USER_HOOKS = "swarm.user_hooks"
CODE_SKILL_USE = "swarm.code_skill_use"

# Key under ``ctx.extras`` where the main agent's CodingMemoryRail is published
# for the code_agent sub-agent to reuse the same instance.
CODING_MEMORY_EXTRAS_KEY = "_coding_memory_rail"

_TEAM_PLAN_EXIT_NOTIFICATION = """\
<system-reminder>
The user approved the team plan. Continue as the Team Leader inside the
team runtime. Do not implement directly as a single code agent. Start the
approved team workflow with team tools such as build_team, create_task,
spawn_teammate, and send_message.
</system-reminder>"""


def _is_team_plan_leader(ctx: SwarmBuildContext) -> bool:
    """Return whether this build context is the team.plan leader."""
    return ctx.mode == "team.plan" and ctx.role == "leader"


def code_runtime_language(ctx: SwarmBuildContext) -> str:
    """Resolve the code member's runtime-prompt language.

    Code mode is English-only except the team.plan leader, which uses the
    configured preferred language (mirrors the legacy ``force_english_runtime_prompt``
    / ``team_plan_runtime_language`` handling).

    Args:
        ctx: The per-member build context.

    Returns:
        The resolved language code ("en" or the configured preferred language).
    """
    if _is_team_plan_leader(ctx):
        return resolve_language((ctx.config or {}).get("preferred_language", "zh"))
    return "en"


def structured_ask_user_language(ctx: SwarmBuildContext) -> str:
    """Resolve the StructuredAskUserRail language for team/code profiles."""
    if ctx.role == "leader" and ctx.mode in {"team", "team.plan"}:
        return resolve_language((ctx.config or {}).get("preferred_language", "zh"))
    return code_runtime_language(ctx)


def _project_dir(ctx: SwarmBuildContext) -> str:
    """Resolve the code project directory from the build context."""
    workspace_root = (
        getattr(ctx.workspace, "root_path", None) if ctx.workspace else None
    )
    return ctx.project_dir or workspace_root or "./"


def _workspace_root(ctx: SwarmBuildContext) -> str | None:
    """Resolve the member workspace root path (None when absent)."""
    return getattr(ctx.workspace, "root_path", None) if ctx.workspace else None


def _code_agent_workspace_dir(ctx: SwarmBuildContext) -> str:
    """Resolve the CodeAgentRail workspace dir (workspace root or project dir)."""
    return str(_workspace_root(ctx) or _project_dir(ctx))


class CodeRuntimePromptInput(ConstructionInput):
    """Construction inputs for the code runtime prompt rail."""

    language: str = context_field(
        resolver=code_runtime_language,
        default="en",
        description="Code runtime language (English, or plan-leader preferred language).",
    )
    channel: str = context_field(
        attr="channel", default="default", description="Resolved channel key."
    )
    project_dir: str | None = context_field(
        attr="project_dir",
        description="Resolved user project directory.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_RUNTIME_PROMPT,
    description="Code-mode runtime prompt rail (English, or the configured language "
    "for the team.plan leader).",
    input_model=CodeRuntimePromptInput,
)
def build_code_runtime_prompt(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the code RuntimePromptRail (English, or configured lang for plan leader)."""
    from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
        RuntimePromptRail,
    )

    try:
        inp = CodeRuntimePromptInput.resolve(params, ctx)
        rail = RuntimePromptRail(language=inp.language, channel=inp.channel)
        # Code-team members need the same per-request runtime binding as the
        # regular team profile; otherwise mode falls back to "unknown".
        rail.set_mode(ctx.mode)
        rail.set_session_id(ctx.session_id)
        rail.set_runtime_paths(cwd=inp.project_dir, project_dir=inp.project_dir)
        return rail
    except Exception as exc:
        logger.warning("[swarm.code_runtime_prompt] create failed: %s", exc)
        return None


class CodeProjectMemoryInput(ConstructionInput):
    """Construction inputs for the code project-memory rail."""

    project_dir: str = context_field(
        resolver=_project_dir,
        default="./",
        description="Project root scanned for memory files.",
    )
    language: str = context_field(
        resolver=code_runtime_language,
        default="en",
        description="Code runtime language.",
    )
    additional_directories: list[str] = param_field(
        default_factory=list,
        description="Extra memory directories from config (react.project_memory) or env.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_PROJECT_MEMORY,
    description="Project memory rail (auto-loads JIUWENSWARM.md / CLAUDE.md and any "
    "additional configured directories).",
    input_model=CodeProjectMemoryInput,
)
def build_code_project_memory(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build ProjectMemoryRail (auto-loads JIUWENSWARM.md / CLAUDE.md etc.)."""
    from jiuwenswarm.agents.harness.common.rails import ProjectMemoryRail

    try:
        inp = CodeProjectMemoryInput.resolve(params, ctx)
        return ProjectMemoryRail(
            workspace=inp.project_dir,
            language=inp.language,
            additional_directories=tuple(inp.additional_directories),
        )
    except Exception as exc:
        logger.warning("[swarm.code_project_memory] create failed: %s", exc)
        return None


class PermissionInterruptInput(ConstructionInput):
    """Construction inputs for the permission-interrupt rail."""

    permissions_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Permission settings (enabled / tools / rules) from config.permissions.",
    )
    model_name: str = param_field(
        default="gpt-4",
        description="Model name from config models.default (drives permission policy).",
    )


class _TeamPlanPermissionInterruptRail:
    """Delegate permission checks while letting plan approval own exit_plan_mode."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.priority = getattr(wrapped, "priority", 90)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def init(self, agent: Any) -> None:
        init = getattr(self._wrapped, "init", None)
        if callable(init):
            init(agent)

    def uninit(self, agent: Any) -> None:
        uninit = getattr(self._wrapped, "uninit", None)
        if callable(uninit):
            uninit(agent)

    def get_callbacks(self) -> dict[Any, Any]:
        callbacks = dict(self._wrapped.get_callbacks())
        from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

        if AgentCallbackEvent.BEFORE_TOOL_CALL in callbacks:
            callbacks[AgentCallbackEvent.BEFORE_TOOL_CALL] = self.before_tool_call
        return callbacks

    async def before_tool_call(self, ctx: Any) -> None:
        inputs = getattr(ctx, "inputs", None)
        tool_name = str(getattr(inputs, "tool_name", "") or "").strip()
        if tool_name == "exit_plan_mode":
            return
        await self._wrapped.before_tool_call(ctx)


@harness_element(
    kind=ElementKind.RAIL,
    name=PERMISSION_INTERRUPT,
    description="Permission-interrupt rail; mounted only when permissions are enabled "
    "in config.",
    input_model=PermissionInterruptInput,
)
def build_permission_interrupt(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build PermissionInterruptRail (None unless ``permissions.enabled`` in config)."""
    try:
        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            PermissionRailBuildOptions,
            build_permission_rail,
        )

        inp = PermissionInterruptInput.resolve(params, ctx)
        rail = build_permission_rail(
            config={"permissions": inp.permissions_config},
            options=PermissionRailBuildOptions(
                llm=None,
                model_name=inp.model_name,
            ),
        )
        if rail is not None and _is_team_plan_leader(ctx):
            return _TeamPlanPermissionInterruptRail(rail)
        return rail
    except Exception as exc:
        logger.warning("[swarm.permission_interrupt] create failed: %s", exc)
        return None


class CodeCodingMemoryInput(ConstructionInput):
    """Construction inputs for the coding-memory rail."""

    project_dir: str | None = context_field(
        attr="project_dir", description="Code project directory."
    )
    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root (defaults to ./ when absent).",
    )
    embed_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Embedding config (api key / base url / model) from config.embed.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_CODING_MEMORY,
    description="Coding memory rail; also published on the build context so the code "
    "sub-agent reuses the same instance.",
    input_model=CodeCodingMemoryInput,
)
def build_code_coding_memory(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build CodingMemoryRail and publish it on ``ctx.extras`` for code_agent reuse."""
    try:
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
            create_coding_memory_rail,
            _set_workspace_coding_memory_directory,
        )

        inp = CodeCodingMemoryInput.resolve(params, ctx)
        workspace_root = str(inp.workspace_root or "./")
        _set_workspace_coding_memory_directory(
            ctx.workspace,
            project_dir=inp.project_dir,
            agent_workspace_dir=workspace_root,
        )
        rail = create_coding_memory_rail(
            project_dir=inp.project_dir,
            agent_workspace_dir=workspace_root,
            config={"embed": inp.embed_config},
        )
        # Share the instance with the code_agent sub-agent via the build context.
        ctx.extras[CODING_MEMORY_EXTRAS_KEY] = rail
        return rail
    except Exception as exc:
        logger.warning("[swarm.code_coding_memory] create failed: %s", exc)
        return None


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_AGENT_MODE,
    description="Code plan-mode rail (Claude-Code-aligned agent mode).",
)
def build_code_agent_mode(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the code plan-mode rail (Claude-Code-aligned AgentModeRail)."""
    try:
        from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
            CodeAgentModeRail,
        )
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
            _ENTER_PLAN_MODE_INSTRUCTIONS_EN,
            _PLAN_MODE_SYSTEM_NOTE,
        )

        exit_notification = (
            _TEAM_PLAN_EXIT_NOTIFICATION
            if _is_team_plan_leader(ctx)
            else None
        )
        return CodeAgentModeRail(
            allowed_tools=[
                "enter_plan_mode",
                "exit_plan_mode",
                "ask_user",
                "task_tool",
                "read_file",
                "grep",
                "list_files",
                "glob",
                "bash",
                "write_file",
                "edit_file",
            ],
            plan_mode_system_note=_PLAN_MODE_SYSTEM_NOTE,
            enter_plan_instructions=_ENTER_PLAN_MODE_INSTRUCTIONS_EN,
            exit_plan_notification=exit_notification,
        )
    except Exception as exc:
        logger.warning("[swarm.code_agent_mode] create failed: %s", exc)
        return None


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_PLAN_APPROVAL,
    description="Reuses the code.plan exit_plan_mode approval interrupt for the team.plan leader.",
)
def build_team_plan_approval(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the team.plan leader approval rail for ``exit_plan_mode``."""
    try:
        if not _is_team_plan_leader(ctx):
            return None
        from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
            PlanApprovalInterruptRail,
        )

        return PlanApprovalInterruptRail()
    except Exception as exc:
        logger.warning("[swarm.team_plan_approval] create failed: %s", exc)
        return None


class StructuredAskUserInput(ConstructionInput):
    """Construction inputs for the structured ask-user rail."""

    language: str = context_field(
        resolver=structured_ask_user_language,
        default="en",
        description="Structured ask-user language.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=STRUCTURED_ASK_USER,
    description="Structured ask-user rail in the code runtime language.",
    input_model=StructuredAskUserInput,
)
def build_structured_ask_user(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the StructuredAskUserRail in the code runtime language."""
    from jiuwenswarm.agents.harness.common.rails import StructuredAskUserRail

    try:
        if (ctx.request_metadata or {}).get("supports_user_interaction") is False:
            return None
        inp = StructuredAskUserInput.resolve(params, ctx)
        return StructuredAskUserRail(language=inp.language)
    except Exception as exc:
        logger.warning("[swarm.structured_ask_user] create failed: %s", exc)
        return None


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_TASK_PLANNING,
    description="Code-specific task planning rail (Claude-Code-aligned todo tools).",
)
def build_code_task_planning(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the code-specific task planning rail (CC-aligned todo tools)."""
    try:
        from jiuwenswarm.agents.harness.code.rails import CodeTaskPlanningRail

        return CodeTaskPlanningRail()
    except Exception as exc:
        logger.warning("[swarm.code_task_planning] create failed: %s", exc)
        return None


class CodeAgentRailInput(ConstructionInput):
    """Construction inputs for the CodeAgentRail."""

    workspace_dir: str = context_field(
        resolver=_code_agent_workspace_dir,
        default="./",
        description="Workspace dir for /agents custom agents (workspace root or project dir).",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_AGENT_RAIL,
    description="CodeAgentRail managing /agents custom agents, rooted at the workspace.",
    input_model=CodeAgentRailInput,
)
def build_code_agent_rail(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build CodeAgentRail (manages /agents custom agents) rooted at the workspace."""
    try:
        from jiuwenswarm.server.runtime.agent_adapter.code_agent_rail import (
            CodeAgentRail,
        )

        inp = CodeAgentRailInput.resolve(params, ctx)
        return CodeAgentRail(workspace_dir=inp.workspace_dir)
    except Exception as exc:
        logger.warning("[swarm.code_agent_rail] create failed: %s", exc)
        return None


class UserHooksInput(ConstructionInput):
    """Construction inputs for the user-hook rail."""

    hooks_section: dict[str, Any] = param_field(
        default_factory=dict,
        description="User hooks config section (config.hooks); built when it declares events.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=USER_HOOKS,
    description="User hook rail built from config.hooks (skipped when no events are "
    "configured).",
    input_model=UserHooksInput,
)
def build_user_hooks(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build UserHookRail from ``config.hooks`` (None when no events are configured)."""
    try:
        from jiuwenswarm.common.hooks_config import load_hooks_config
        from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail

        inp = UserHooksInput.resolve(params, ctx)
        hooks_config = load_hooks_config({"hooks": inp.hooks_section})
        if getattr(hooks_config, "events", None):
            return UserHookRail(hooks_config)
        return None
    except Exception as exc:
        logger.warning("[swarm.user_hooks] create failed: %s", exc)
        return None


class CodeSkillUseInput(ConstructionInput):
    """Construction inputs for the code skill-use rail."""

    skill_mode: str = param_field(
        default=SkillUseRail.SKILL_MODE_ALL,
        description="Skill exposure mode (ALL or AUTO_LIST) from react.skill_mode.",
    )
    include_tools: bool = param_field(
        default=True,
        description="Whether SkillUseRail should expose regular skill tools.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=CODE_SKILL_USE,
    description="Skill-use rail from the config source (skill_mode plus disabled skills).",
    input_model=CodeSkillUseInput,
)
def build_code_skill_use(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build SkillUseRail from the config source (skill_mode + disabled skills)."""
    from jiuwenswarm.common.utils import get_agent_skills_dir
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
        is_skill_retrieval_enabled,
    )
    from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

    try:
        inp = CodeSkillUseInput.resolve(params, ctx)
        skill_mode = (
            SkillUseRail.SKILL_MODE_AUTO_LIST
            if is_skill_retrieval_enabled()
            else inp.skill_mode
        )
        return SkillUseRail(
            skills_dir=str(get_agent_skills_dir()),
            skill_mode=skill_mode,
            include_tools=inp.include_tools,
            disabled_skills=load_execution_disabled_skills(),
        )
    except Exception as exc:
        logger.warning("[swarm.code_skill_use] create failed: %s", exc)
        return None


__all__ = [
    "CODE_RUNTIME_PROMPT",
    "CODE_PROJECT_MEMORY",
    "PERMISSION_INTERRUPT",
    "CODE_CODING_MEMORY",
    "CODE_AGENT_MODE",
    "TEAM_PLAN_APPROVAL",
    "STRUCTURED_ASK_USER",
    "CODE_TASK_PLANNING",
    "CODE_AGENT_RAIL",
    "USER_HOOKS",
    "CODE_SKILL_USE",
    "CODING_MEMORY_EXTRAS_KEY",
    "code_runtime_language",
    "structured_ask_user_language",
]
