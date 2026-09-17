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

from typing import Any

from openjiuwen.agent_teams.harness.manifest import register_from_catalog
from openjiuwen.agent_teams.rails.builtin_elements import (
    AUDIO as _OJ_AUDIO,
    CONFIRM_INTERRUPT as _OJ_CONFIRM_INTERRUPT,
    HEARTBEAT as _OJ_HEARTBEAT,
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
from jiuwenswarm.agents.swarm.providers import (
    builtin_rails as _builtin_rails,
    code_rails as _code_rails,
    code_subagents as _code_subagents,
    evolution_rails as _evolution_rails,
    member_rails as _member_rails,
    runtime_tools as _runtime_tools,
    skills as _skills,
    tools as _tools,
)
from jiuwenswarm.common.config import get_config

# Re-exported provider name constants for config_specs to reference by symbol.
# Swarm-owned tools (each self-gated by config + whitelist-filtered).
SKILL_TOOLKIT = _tools.SKILL_TOOLKIT
SKILL_RETRIEVAL = _tools.SKILL_RETRIEVAL
USER_TODOS = _tools.USER_TODOS
VIDEO = _tools.VIDEO
IMAGE_GEN = _tools.IMAGE_GEN
XIAOYI_PHONE = _tools.XIAOYI_PHONE
SYMPHONY_TOOLKIT = _tools.SYMPHONY_TOOLKIT
CRON_TOOLS = _runtime_tools.CRON_TOOLS
SEND_FILE = _runtime_tools.SEND_FILE
MEMBER_SKILL_TOOLKIT = _skills.MEMBER_SKILL_TOOLKIT
# Generic tools provided + registered by openjiuwen (referenced by bare name).
WEB_SEARCH = _OJ_WEB_SEARCH
WEB_FETCH = _OJ_WEB_FETCH
WEB_PAID_SEARCH = _OJ_WEB_PAID_SEARCH
VISION = _OJ_VISION
AUDIO = _OJ_AUDIO
RUNTIME_PROMPT = _member_rails.RUNTIME_PROMPT
TEAM_SKILL_STORAGE_POLICY = _member_rails.TEAM_SKILL_STORAGE_POLICY
TEAM_SKILL_LIBRARY_RELOAD = _member_rails.TEAM_SKILL_LIBRARY_RELOAD
TEAM_WORKSPACE_REPORT_PATH = _member_rails.TEAM_WORKSPACE_REPORT_PATH
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

# Code-profile (code.team / team.plan.code) swarm-owned rail provider names.
CODE_EXTRA_TOOLS = _tools.CODE_EXTRA_TOOLS
CODE_RUNTIME_PROMPT = _code_rails.CODE_RUNTIME_PROMPT
CODE_PROJECT_MEMORY = _code_rails.CODE_PROJECT_MEMORY
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

# Swarm-owned no-parameter class rails declared in ``builtin_rails``.
RESPONSE_PROMPT = _builtin_rails.RESPONSE_PROMPT
STREAM_EVENT = _builtin_rails.STREAM_EVENT
AVATAR_PROMPT = _builtin_rails.AVATAR_PROMPT
MULTIMODAL_IMAGE = _builtin_rails.MULTIMODAL_IMAGE

# Generic rails provided + registered by openjiuwen (referenced by bare name).
SYS_OPERATION = _OJ_SYS_OPERATION
TASK_PLANNING = _OJ_TASK_PLANNING
SUBAGENT = _OJ_SUBAGENT
SECURITY = _OJ_SECURITY
HEARTBEAT = _OJ_HEARTBEAT
CODE_LSP = _OJ_LSP
CODE_CONFIRM_INTERRUPT = _OJ_CONFIRM_INTERRUPT
CODE_WORKTREE = _OJ_WORKTREE

_REGISTERED = False


def _build_swarm_context_from_seed(seed: dict[str, Any]) -> SwarmBuildContext:
    """Rebuild a :class:`SwarmBuildContext` from a serializable seed.

    Sources the non-serializable handles from the receiving process: ``config``
    from this process's ``config.yaml`` and the process-level Team trajectory
    span processor.
    Registered with openjiuwen so ``from_spawn_payload`` / ``recover_from_session``
    restore the provider build context after deserialization.
    """
    from jiuwenswarm.agents.harness.observability_runtime import (
        get_trajectory_span_processor,
    )

    return SwarmBuildContext.from_seed(
        seed,
        config=get_config(),
        trajectory_span_processor=get_trajectory_span_processor(),
    )


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

    _REGISTERED = True


__all__ = [
    "register_swarm_providers",
    "SKILL_TOOLKIT",
    "SKILL_RETRIEVAL",
    "USER_TODOS",
    "VIDEO",
    "IMAGE_GEN",
    "XIAOYI_PHONE",
    "SYMPHONY_TOOLKIT",
    "WEB_SEARCH",
    "WEB_FETCH",
    "WEB_PAID_SEARCH",
    "VISION",
    "AUDIO",
    "CRON_TOOLS",
    "SEND_FILE",
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
    "TASK_PLANNING",
    "SECURITY",
    "HEARTBEAT",
    "AVATAR_PROMPT",
    "CODE_EXTRA_TOOLS",
    "CODE_RUNTIME_PROMPT",
    "CODE_LSP",
    "CODE_PROJECT_MEMORY",
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
    "BROWSER_AGENT",
]
