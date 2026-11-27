# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Central registration for swarm capability providers and rail types.

``register_swarm_providers`` wires the whole provider-based assembly into
openjiuwen's registries once per process. Importing the provider modules runs
their ``@harness_element`` declarations, which populate the manifest catalog
(the single source of truth for element metadata). ``register_swarm_providers``
then drives the openjiuwen registration from that catalog via
``register_from_catalog`` — tools, factory rails, the unified class rails (all
through ``register_rail_provider``), and sub-agents — and registers the
build-context seed factory.

The provider name constants are re-exported here so ``config_specs`` can build
``RailSpec`` / ``BuiltinToolSpec`` references without hard-coding strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openjiuwen.agent_teams.harness.manifest import register_from_catalog
from openjiuwen.agent_teams.rails.builtin_elements import (
    AUDIO as _OJ_AUDIO,
    CONFIRM_INTERRUPT as _OJ_CONFIRM_INTERRUPT,
    LSP as _OJ_LSP,
    SECURITY as _OJ_SECURITY,
    SUBAGENT as _OJ_SUBAGENT,
    SYS_OPERATION as _OJ_SYS_OPERATION,
    TASK_PLANNING as _OJ_TASK_PLANNING,
    VISION as _OJ_VISION,
    WEB_FETCH as _OJ_WEB_FETCH,
    WEB_PAID_SEARCH as _OJ_WEB_PAID_SEARCH,
    WEB_SEARCH as _OJ_WEB_SEARCH,
    WORKTREE as _OJ_WORKTREE,
)
from openjiuwen.agent_teams.rails.registration import (
    ensure_harness_elements_registered,
)
from openjiuwen.agent_teams.rails.subagent_elements import (
    BROWSER_AGENT as _OJ_BROWSER_AGENT,
    EXPLORE_AGENT as _OJ_EXPLORE_AGENT,
    PLAN_AGENT as _OJ_PLAN_AGENT,
)
from openjiuwen.agent_teams.schema.build_context import register_build_context_factory

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm import group_chat as _group_chat  # noqa: F401 -- registers tool providers
from jiuwenswarm.agents.swarm import group_execution as _group_execution  # noqa: F401 -- registers task providers
from jiuwenswarm.agents.swarm import group_org_lifecycle as _group_org_lifecycle  # noqa: F401 -- registers org policy
from jiuwenswarm.agents.swarm.providers import (
    builtin_rails as _builtin_rails,
    code_rails as _code_rails,
    code_subagents as _code_subagents,
    design_rails as _design_rails,
    designer_tools as _designer_tools,  # noqa: F401  # imported for provider registration
    evolution_rails as _evolution_rails,
    member_rails as _member_rails,
    team_memory as _team_memory,
    runtime_tools as _runtime_tools,
    skills as _skills,
    tools as _tools,
)
from jiuwenswarm.common.config import get_config

if TYPE_CHECKING:
    # String-annotated only: importing core's expert module at module level would
    # make every registry import (all team assembly) require the newest core.
    from openjiuwen.agent_teams.experts.template_connectors import ResolvedConnectors

# The event-mode rollout is explicit at the Swarm assembly boundary. The
# config-spec adapter gates this value on the agent-core field so an older Core
# keeps the legacy direct-registration path.
TOOL_REGISTRATION_MODE = "event"

#一期默认通过 agent.register 的 SubAgentConfig 载荷注册。其它类型仍可
#继续由 provider 产出并走 legacy，直到宿主策略为其单独打开事件路径。
SUBAGENT_REGISTRATION_MODE = "event"
EVENT_SUBAGENT_TYPES = (
    "statusline-setup",
    "general-purpose",
    "explore_agent",
    "plan_agent",
)

# Re-exported provider name constants for config_specs to reference by symbol.
# Swarm-owned tools (each self-gated by config + whitelist-filtered).
GROUP_RUN_TEAM = _group_chat.RUN_TEAM
GROUP_COMPLETE_TEAM_RUN = _group_chat.COMPLETE_RUN
GROUP_MEMBER_TEMPLATE = _group_chat.GROUP_MEMBER_TEMPLATE
GROUP_PRIVATE_TASK_POLICY = _group_chat.PRIVATE_TASK_POLICY
GROUP_BEGIN_TEAM_TASK = _group_chat.BEGIN_TEAM_TASK
GROUP_START_TASK = _group_execution.START_TASK
GROUP_TASK_POLICY = _group_execution.TASK_POLICY
GROUP_ASK_USER = _group_execution.GROUP_ASK_USER

SKILL_TOOLKIT = _tools.SKILL_TOOLKIT
SKILL_RETRIEVAL = _tools.SKILL_RETRIEVAL
USER_TODOS = _tools.USER_TODOS
VIDEO = _tools.VIDEO
IMAGE_GEN = _tools.IMAGE_GEN
VIDEO_GEN = _tools.VIDEO_GEN
VISUAL_GEN = _tools.VISUAL_GEN
XIAOYI_PHONE = _tools.XIAOYI_PHONE
SYMPHONY_TOOLKIT = _tools.SYMPHONY_TOOLKIT
A4P_INTENT_AUTHORIZATION = _tools.A4P_INTENT_AUTHORIZATION
DESIGNER_GRAPH_TOOLS = "swarm.designer_graph_tools"
DESIGNER_AUTHORING = _design_rails.DESIGNER_AUTHORING
DESIGNER_EXECUTION = _design_rails.DESIGNER_EXECUTION
DESIGNER_SURFACE_SANITIZER = _design_rails.DESIGNER_SURFACE_SANITIZER
CRON_TOOLS = _runtime_tools.CRON_TOOLS
SEND_FILE = _runtime_tools.SEND_FILE
SESSION_MESSAGING_TOOLS = _runtime_tools.SESSION_MESSAGING_TOOLS
ORGANIZATION_MODEL_TOOLS = _runtime_tools.ORGANIZATION_MODEL_TOOLS
ORGANIZATION_OWNER_INBOX_TOOLS = _runtime_tools.ORGANIZATION_OWNER_INBOX_TOOLS
MEMBER_SKILL_TOOLKIT = _skills.MEMBER_SKILL_TOOLKIT
# Generic tools provided + registered by openjiuwen (referenced by bare name).
WEB_SEARCH = _OJ_WEB_SEARCH
WEB_FETCH = _OJ_WEB_FETCH
WEB_PAID_SEARCH = _OJ_WEB_PAID_SEARCH
VISION = _OJ_VISION
AUDIO = _OJ_AUDIO
# Only these core providers are migrated in the first event-registration
# slice. Swarm-owned providers deliberately remain on the legacy path.
EVENT_TOOL_TYPES = (WEB_SEARCH, WEB_FETCH, WEB_PAID_SEARCH, VISION, AUDIO)
TEAM_MEMORY = _team_memory.TEAM_MEMORY
PERSONAL_CONTEXT = _member_rails.PERSONAL_CONTEXT
RUNTIME_PROMPT = _member_rails.RUNTIME_PROMPT
TEAM_SKILL_STORAGE_POLICY = _member_rails.TEAM_SKILL_STORAGE_POLICY
TEAM_SKILL_LIBRARY_RELOAD = _member_rails.TEAM_SKILL_LIBRARY_RELOAD
TEAM_WORKSPACE_REPORT_PATH = _member_rails.TEAM_WORKSPACE_REPORT_PATH
ORGANIZATION_AVAILABILITY = _member_rails.ORGANIZATION_AVAILABILITY
ORGANIZATION_MODEL = _member_rails.ORGANIZATION_MODEL
ORGANIZATION_OWNER_INBOX = _member_rails.ORGANIZATION_OWNER_INBOX
OPENCODE_SESSION = _member_rails.OPENCODE_SESSION
CONTEXT_PROCESSOR = _member_rails.CONTEXT_PROCESSOR
MODEL_ANOMALY_DETECTION = _member_rails.MODEL_ANOMALY_DETECTION
PLUGIN_RAILS = _member_rails.PLUGIN_RAILS
SKILL_RETRIEVAL_PROMPT = _member_rails.SKILL_RETRIEVAL_PROMPT
SYMPHONY_ORCHESTRATION_PROMPT = _member_rails.SYMPHONY_ORCHESTRATION_PROMPT
TEAM_PERMISSION = _member_rails.TEAM_PERMISSION
TEAM_PERMISSION_POLICY = _member_rails.TEAM_PERMISSION_POLICY
TEAM_SKILL_EVOLUTION = _evolution_rails.TEAM_SKILL_EVOLUTION
TEAM_SKILL_CREATE = _evolution_rails.TEAM_SKILL_CREATE
MEMBER_SKILL_EVOLUTION = _evolution_rails.MEMBER_SKILL_EVOLUTION
EVOLUTION_INTERRUPT = _evolution_rails.EVOLUTION_INTERRUPT
SYMPHONY_GRAPH_EVOLUTION = _evolution_rails.SYMPHONY_GRAPH_EVOLUTION

# Code-profile (code.team / team.plan.code) swarm-owned rail provider names.
CODE_EXTRA_TOOLS = _tools.CODE_EXTRA_TOOLS
CODE_RUNTIME_PROMPT = _code_rails.CODE_RUNTIME_PROMPT
CODE_PROJECT_MEMORY = _code_rails.CODE_PROJECT_MEMORY
CODE_AGENT_MD = _code_rails.CODE_AGENT_MD
PERMISSION_INTERRUPT = _code_rails.PERMISSION_INTERRUPT
CODE_CODING_MEMORY = _code_rails.CODE_CODING_MEMORY
CODE_AGENT_MODE = _code_rails.CODE_AGENT_MODE
TEAM_PLAN_APPROVAL = _code_rails.TEAM_PLAN_APPROVAL
STRUCTURED_ASK_USER = _code_rails.STRUCTURED_ASK_USER
CODE_TASK_PLANNING = _code_rails.CODE_TASK_PLANNING
CODE_AGENT_RAIL = _code_rails.CODE_AGENT_RAIL
USER_HOOKS = _code_rails.USER_HOOKS

# Sub-agent provider names (resolved via SubAgentSpec.factory_name). explore /
# plan / browser are provided by openjiuwen; code_agent stays swarm-side (reuses
# the swarm CodingMemoryRail).
EXPLORE_AGENT = _OJ_EXPLORE_AGENT
PLAN_AGENT = _OJ_PLAN_AGENT
BROWSER_AGENT = _OJ_BROWSER_AGENT
CODE_AGENT = _code_subagents.CODE_AGENT
DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS = (
    _code_subagents.DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS
)
STATUSLINE_SETUP_AGENT = _code_subagents.STATUSLINE_SETUP_AGENT
SWARM_BROWSER_AGENT = _code_subagents.SWARM_BROWSER_AGENT
SWARM_CUA_AGENT = _code_subagents.SWARM_CUA_AGENT

# Swarm-owned no-parameter class rails declared in ``builtin_rails``.
RESPONSE_PROMPT = _builtin_rails.RESPONSE_PROMPT
STREAM_EVENT = _builtin_rails.STREAM_EVENT
AVATAR_PROMPT = _builtin_rails.AVATAR_PROMPT
MULTIMODAL_IMAGE = _builtin_rails.MULTIMODAL_IMAGE
WORKSPACE_QUOTA = _builtin_rails.WORKSPACE_QUOTA

# Generic rails provided + registered by openjiuwen (referenced by bare name).
SYS_OPERATION = _OJ_SYS_OPERATION
TASK_PLANNING = _OJ_TASK_PLANNING
SUBAGENT = _OJ_SUBAGENT
SECURITY = _OJ_SECURITY
HEARTBEAT = _member_rails.HEARTBEAT
SESSION_MESSAGING_ROUTE = _member_rails.SESSION_MESSAGING_ROUTE
CODE_LSP = _OJ_LSP
CODE_CONFIRM_INTERRUPT = _OJ_CONFIRM_INTERRUPT
CODE_WORKTREE = _OJ_WORKTREE

_REGISTERED = False


def supports_tool_registration_event() -> bool:
    """Return whether the installed agent-core exposes the event bridge.

    The field check alone is insufficient during a rolling two-repository
    upgrade: the event context, DeepAgent queue, and EventBus signature must
    all be available before Swarm opts in. Any incomplete capability keeps the
    legacy path active.
    """
    try:
        import inspect

        from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
        from openjiuwen.core.events import ExtensionEventBus
        from openjiuwen.core.events.builtins.event_types.tool_register import (
            ToolRegisterTriggerContext,
        )
        from openjiuwen.harness.deep_agent import DeepAgent

        fields = getattr(DeepAgentSpec, "model_fields", {})
        trigger_params = inspect.signature(ExtensionEventBus.trigger).parameters
        return (
            {"tool_registration_mode", "event_tool_types"}.issubset(fields)
            and ToolRegisterTriggerContext is not None
            and callable(getattr(DeepAgent, "queue_tool_registration", None))
            and callable(getattr(DeepAgent, "_flush_tool_registrations", None))
            and "trigger_context" in trigger_params
        )
    except (AttributeError, ImportError, TypeError, ValueError):
        return False


def supports_subagent_registration_event() -> bool:
    """Return whether the installed agent-core supports startup subagent events.

    This gate intentionally checks the complete bridge rather than a single
    dataclass field so JiuwenSwarm can run against an older local agent-core
    during a rolling two-worktree upgrade.
    """
    try:
        import inspect

        from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
        from openjiuwen.core.events import ExtensionEventBus
        from openjiuwen.core.events.builtins.event_types.agent_register import (
            AgentRegisterInput,
            AgentRegisterTriggerContext,
        )
        from openjiuwen.harness.deep_agent import DeepAgent

        fields = getattr(DeepAgentSpec, "model_fields", {})
        input_fields = getattr(AgentRegisterInput, "__dataclass_fields__", {})
        trigger_params = inspect.signature(ExtensionEventBus.trigger).parameters
        return (
            {"subagent_registration_mode", "event_subagent_types"}.issubset(fields)
            and "subagent_config" in input_fields
            and callable(getattr(AgentRegisterInput, "from_subagent_config", None))
            and AgentRegisterTriggerContext is not None
            and callable(getattr(DeepAgent, "_queue_subagent_registration", None))
            and callable(getattr(DeepAgent, "_flush_subagent_registrations", None))
            and callable(getattr(DeepAgent, "load_subagent_config", None))
            and "trigger_context" in trigger_params
        )
    except (AttributeError, ImportError, TypeError, ValueError):
        return False


def _build_swarm_context_from_seed(seed: dict[str, Any]) -> SwarmBuildContext:
    """Rebuild a :class:`SwarmBuildContext` from a serializable seed.

    Sources the non-serializable handles from the receiving process: ``config``
    from this process's ``config.yaml`` and the process-level Team trajectory
    span processor.
    Registered with openjiuwen so ``from_spawn_payload`` / ``recover_from_session``
    restore the provider build context after deserialization.
    """
    from openjiuwen.extensions.observability.demand import (
        get_trajectory_span_processor,
    )
    try:
        from openjiuwen.core.runner import Runner

        extension_bus = Runner.get_extension_bus()
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        extension_bus = None

    return SwarmBuildContext.from_seed(
        seed,
        config=get_config(),
        trajectory_span_processor=get_trajectory_span_processor(),
        extension_bus=extension_bus,
    )


def _disabled_skill_names() -> set[str]:
    """Skills the user switched off (``skill_configs[*].enabled is False``).

    Read on the swarm side (which may import the server layer) and passed into
    :func:`resolve_connectors` so ``common`` never imports ``server`` (rule R1).
    """
    import json

    from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
        get_state_file,
        list_disabled_skills,
    )

    try:
        state = json.loads(get_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(list_disabled_skills(state)) if isinstance(state, dict) else set()


def _resolve_template_connectors(
    names: list[str], team_name: str, member_name: str
) -> "ResolvedConnectors":
    """Resolve an expert member's template connectors into MCPs and Skill dirs.

    Scoped per team member so two experts binding the same connector each get
    their own stable ``server_id``. Skills the user switched off are excluded.
    """
    from openjiuwen.agent_teams.experts.template_connectors import ResolvedConnectors

    from jiuwenswarm.common.mcp_config import resolve_connectors

    mcps, skill_dirs = resolve_connectors(
        names,
        server_id_scope=f"team:{team_name}:{member_name}",
        disabled_skills=_disabled_skill_names(),
    )
    return ResolvedConnectors(mcps=mcps, skill_dirs=skill_dirs)


def register_swarm_providers() -> None:
    """Register all swarm providers and rail types with openjiuwen (idempotent).

    Importing this module (and the provider modules above) runs the
    ``@harness_element`` declarations that populate the manifest catalog. This
    function drives the actual openjiuwen registration from that catalog and
    wires the build-context seed factory. Safe to call multiple times, but only
    the first call performs the registration.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    # Ensure the openjiuwen built-in rails / tools / sub-agents this platform
    # references by bare name (sys_operation / task_planning / confirm_interrupt /
    # worktree / lsp / explore_agent / ...) are declared and registered.
    ensure_harness_elements_registered()
    register_from_catalog()
    register_build_context_factory(_build_swarm_context_from_seed)
    # Expert members mount the MCP connectors bound on their AgentTemplate.
    from openjiuwen.agent_teams.experts.template_connectors import (
        register_template_connector_resolver,
    )

    register_template_connector_resolver(_resolve_template_connectors)

    _REGISTERED = True


__all__ = [
    "GROUP_RUN_TEAM",
    "GROUP_COMPLETE_TEAM_RUN",
    "GROUP_MEMBER_TEMPLATE",
    "GROUP_PRIVATE_TASK_POLICY",
    "GROUP_BEGIN_TEAM_TASK",
    "GROUP_START_TASK",
    "GROUP_TASK_POLICY",
    "GROUP_ASK_USER",

    "TEAM_MEMORY",
    "PERSONAL_CONTEXT",
    "register_swarm_providers",
    "SKILL_TOOLKIT",
    "SKILL_RETRIEVAL",
    "USER_TODOS",
    "VIDEO",
    "IMAGE_GEN",
    "VIDEO_GEN",
    "VISUAL_GEN",
    "XIAOYI_PHONE",
    "SYMPHONY_TOOLKIT",
    "WEB_SEARCH",
    "WEB_FETCH",
    "WEB_PAID_SEARCH",
    "VISION",
    "AUDIO",
    "EVENT_TOOL_TYPES",
    "supports_tool_registration_event",
    "SUBAGENT_REGISTRATION_MODE",
    "EVENT_SUBAGENT_TYPES",
    "supports_subagent_registration_event",
    "CRON_TOOLS",
    "SEND_FILE",
    "SESSION_MESSAGING_TOOLS",
    "ORGANIZATION_MODEL_TOOLS",
    "ORGANIZATION_OWNER_INBOX_TOOLS",
    "ORGANIZATION_OWNER_INBOX",
    "ORGANIZATION_MODEL",
    "MEMBER_SKILL_TOOLKIT",
    "RUNTIME_PROMPT",
    "TEAM_SKILL_STORAGE_POLICY",
    "TEAM_SKILL_LIBRARY_RELOAD",
    "TEAM_WORKSPACE_REPORT_PATH",
    "CONTEXT_PROCESSOR",
    "MODEL_ANOMALY_DETECTION",
    "PLUGIN_RAILS",
    "SKILL_RETRIEVAL_PROMPT",
    "SYMPHONY_ORCHESTRATION_PROMPT",
    "TEAM_PERMISSION",
    "TEAM_PERMISSION_POLICY",
    "TEAM_SKILL_EVOLUTION",
    "TEAM_SKILL_CREATE",
    "MEMBER_SKILL_EVOLUTION",
    "RESPONSE_PROMPT",
    "SYS_OPERATION",
    "STREAM_EVENT",
    "MULTIMODAL_IMAGE",
    "WORKSPACE_QUOTA",
    "TASK_PLANNING",
    "SECURITY",
    "HEARTBEAT",
    "SESSION_MESSAGING_ROUTE",
    "AVATAR_PROMPT",
    "CODE_EXTRA_TOOLS",
    "CODE_RUNTIME_PROMPT",
    "CODE_LSP",
    "CODE_PROJECT_MEMORY",
    "CODE_AGENT_MD",
    "PERMISSION_INTERRUPT",
    "CODE_CODING_MEMORY",
    "CODE_AGENT_MODE",
    "TEAM_PLAN_APPROVAL",
    "STRUCTURED_ASK_USER",
    "CODE_CONFIRM_INTERRUPT",
    "CODE_TASK_PLANNING",
    "CODE_AGENT_RAIL",
    "USER_HOOKS",
    "CODE_WORKTREE",
    "EXPLORE_AGENT",
    "PLAN_AGENT",
    "CODE_AGENT",
    "DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS",
    "STATUSLINE_SETUP_AGENT",
    "SWARM_BROWSER_AGENT",
    "SWARM_CUA_AGENT",
    "BROWSER_AGENT",
    "TOOL_REGISTRATION_MODE",
]
