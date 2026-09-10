# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-sourced capability specs for swarm team members.

This module is the rewritten "member capability" assembly: a member's rails,
tools and sub-agents are declared entirely as ``RailSpec`` / ``BuiltinToolSpec``
/ ``SubAgentSpec`` references to ``swarm.*`` providers, derived from the config
source and the member role. It deliberately depends on **no DeepAgent instance**
— only on the config mapping and the provider name constants re-exported by
:mod:`jiuwenswarm.agents.swarm.registry`.

``build_member_deep_agent_spec`` folds those specs onto a base ``DeepAgentSpec``
so openjiuwen builds the member from the merged spec. Two profiles are supported:

* ``team`` — the chat team profile (common rails + base tools).
* ``code.team`` / ``team.plan.code`` — the code profile (``swarm.code_*`` rails, code
    sub-agents, code system prompt), all still purely declarative.
"""

from __future__ import annotations

import os
from typing import (
    Any,
    Callable,
)

from openjiuwen.agent_teams.schema.deep_agent_spec import (
    BuiltinToolSpec,
    DeepAgentSpec,
    RailSpec,
    SubAgentSpec,
)
from openjiuwen.agent_teams.rails.builtin_elements import SKILL_USE as CORE_SKILL_USE
from openjiuwen.agent_teams.rails.elements import TEAM_SKILL_USE
from openjiuwen.core.foundation.kv_cache import KVCacheAffinityConfig
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.common.config import (
    get_default_model_provider,
    get_evolution_auto_save_enabled,
    get_skill_evolution_enabled,
)
from jiuwenswarm.common.kv_cache_affinity_config import (
    build_kv_cache_affinity_config,
)
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    get_context_engine_enabled,
    resolve_model_config,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.providers import tools as _tools
from jiuwenswarm.common.mode_matrix import (
    NEW_TEAM_CODE_NORMAL,
    NEW_TEAM_CODE_PLAN,
    TEAM_PLAN_CODE_MODE,
    is_team_plan_mode,
)

# Modes that route to the code adapter and get the code member profile.
_CODE_MODES: frozenset[str] = frozenset(
    {
        "code.team",
        TEAM_PLAN_CODE_MODE,
        NEW_TEAM_CODE_NORMAL,
        NEW_TEAM_CODE_PLAN,
    }
)


def _kv_cache_affinity_config(config: dict[str, Any]) -> KVCacheAffinityConfig:
    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    return build_kv_cache_affinity_config(
        react,
        provider=get_default_model_provider(config),
    )


# Rails common to both roles, in mount order. Each entry is a ``swarm.*``
# provider name re-exported from the registry (no hard-coded strings).
_COMMON_RAIL_NAMES: tuple[str, ...] = (
    registry.RUNTIME_PROMPT,
    registry.TEAM_SKILL_STORAGE_POLICY,
    # Reloads the member's Skill view after a write into the single library.
    # Replaces the former link-refresh rail: there is no per-team link view.
    registry.TEAM_SKILL_LIBRARY_RELOAD,
    registry.RESPONSE_PROMPT,
    registry.SYS_OPERATION,
    registry.STREAM_EVENT,
    registry.TASK_PLANNING,
    registry.SECURITY,
    registry.MODEL_ANOMALY_DETECTION,
    registry.HEARTBEAT,
    registry.AVATAR_PROMPT,
    registry.MULTIMODAL_IMAGE,
    registry.TEAM_WORKSPACE_REPORT_PATH,
    registry.CONTEXT_PROCESSOR,
    registry.PLUGIN_RAILS,
    registry.SKILL_RETRIEVAL_PROMPT,
    registry.SYMPHONY_ORCHESTRATION_PROMPT,
)

# Tools common to both roles. Each element self-gates on config, so all are
# declared; an unconfigured element simply yields no tools.
_COMMON_TOOL_NAMES: tuple[str, ...] = (
    registry.WEB_SEARCH,
    registry.WEB_FETCH,
    registry.WEB_PAID_SEARCH,
    registry.VISION,
    registry.AUDIO,
    # Skill-management tools (search/install/uninstall_skill) are registered by
    # the MEMBER_SKILL_TOOLKIT rail (appended below), which owns them and
    # reloads the member's Skill view after a mutation. Declaring SKILL_TOOLKIT
    # here too only double-registers the same-named tools, logging a refresh +
    # duplicate-ability warning per tool every build; the rail is the sole
    # registrar.
    registry.SKILL_RETRIEVAL,
    registry.SYMPHONY_TOOLKIT,
    registry.USER_TODOS,
    registry.VIDEO,
    registry.IMAGE_GEN,
    registry.XIAOYI_PHONE,
    registry.CRON_TOOLS,
    registry.SEND_FILE,
)

# Parameterless code-profile rails (the code variant of the common rails plus
# code-specific rails). ``code_confirm_interrupt`` carries params and is
# appended separately, as is ``member_skill_toolkit``, which is appended next
# to the Skill rail it complements.
_CODE_RAIL_NAMES: tuple[str, ...] = (
    registry.CODE_RUNTIME_PROMPT,
    registry.RESPONSE_PROMPT,
    registry.STREAM_EVENT,
    registry.MULTIMODAL_IMAGE,
    registry.SECURITY,
    registry.MODEL_ANOMALY_DETECTION,
    registry.HEARTBEAT,
    registry.CODE_LSP,
    registry.CODE_PROJECT_MEMORY,
    registry.PERMISSION_INTERRUPT,
    registry.SYS_OPERATION,
    registry.CODE_CODING_MEMORY,
    registry.CODE_AGENT_MODE,
    registry.STRUCTURED_ASK_USER,
    registry.CONTEXT_PROCESSOR,
    registry.CODE_TASK_PLANNING,
    registry.CODE_AGENT_RAIL,
    registry.USER_HOOKS,
    # The Skill rail is appended separately: it is the team-owned
    # ``core.team.skill_use``, shared with the chat profile.
    registry.SKILL_RETRIEVAL_PROMPT,
    registry.SYMPHONY_ORCHESTRATION_PROMPT,
)

# Rails shared with the team profile, appended to the code profile.
_CODE_SHARED_RAIL_NAMES: tuple[str, ...] = (
    registry.TEAM_WORKSPACE_REPORT_PATH,
    registry.PLUGIN_RAILS,
)

# Code member tools: the common tool set plus the code-exclusive acp_chat.
_CODE_TOOL_NAMES: tuple[str, ...] = (
    registry.WEB_SEARCH,
    registry.WEB_FETCH,
    registry.WEB_PAID_SEARCH,
    registry.VISION,
    registry.AUDIO,
    # See _COMMON_TOOL_NAMES: skill tools come from the MEMBER_SKILL_TOOLKIT
    # rail; declaring SKILL_TOOLKIT here too would double-register them.
    registry.SKILL_RETRIEVAL,
    registry.SYMPHONY_TOOLKIT,
    registry.USER_TODOS,
    registry.VIDEO,
    registry.IMAGE_GEN,
    registry.XIAOYI_PHONE,
    registry.CODE_EXTRA_TOOLS,
    registry.CRON_TOOLS,
    registry.SEND_FILE,
)

# code_agent sub-agents are always-on (explore / plan) or config-gated.
_DEFAULT_SUBAGENT_MAX_ITERATIONS = 15


def _is_code_mode(mode: str) -> bool:
    """Return whether *mode* routes to the code member profile."""
    return mode in _CODE_MODES


def _resolve_member_skills(config: dict[str, Any], role: str) -> list[str]:
    """Resolve the seed allow-list for *role* from the config source.

    Reads ``config.agents.<role>.skills`` and returns the cleaned, non-empty
    names.

    This is **no longer the runtime authority**. The value is only the seed for
    a member's ``skills-visibility.json``, carried by the ``core.team.skill_use``
    rail as its ``bootstrap_allow`` and written once — by
    ``openjiuwen.agent_teams.rails.team_skill_use_rail.create_team_skill_use_rail``,
    the single seeder — when the file does not exist yet; afterwards the
    document decides what the member sees and editing this config key changes
    nothing for members that already have one. The value is also per *role*,
    while the document is per member *instance*: the rail resolves the member
    identity at setup time, so nothing else has to expand role to instance.

    Args:
        config: The resolved ``config.yaml`` mapping (team blueprint shape).
        role: The member role ("leader" or "teammate").

    Returns:
        The seed skill names for the role (possibly empty; empty means the
        member inherits the whole Skill library).
    """
    agents = config.get("agents") if isinstance(config, dict) else None
    if not isinstance(agents, dict):
        return []
    member = agents.get(role)
    if not isinstance(member, dict):
        return []
    skills = member.get("skills")
    if not isinstance(skills, list):
        return []
    return [str(skill).strip() for skill in skills if str(skill).strip()]


# ---------------------------------------------------------------------------
# Config-attribute extraction: harness settings (config.yaml) baked into spec
# ``params`` at spec-build time. Per-request environment values stay on the
# build context; only these attributes are projected into ``RailSpec.params`` /
# ``BuiltinToolSpec.params``.
# ---------------------------------------------------------------------------


def _config_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``config[key]`` as a dict (empty when absent or the wrong type)."""
    section = (config or {}).get(key, {})
    return section if isinstance(section, dict) else {}


def _evolution_model_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the serializable evolution model config (the LLM is built later)."""
    model_client_config, model_config_obj, model_name = resolve_model_config(
        config or {}
    )
    return {
        "model_client_config": model_client_config,
        "model_config_obj": model_config_obj,
        "model_name": model_name,
    }


def _skill_mode(config: dict[str, Any]) -> str:
    """Resolve the validated skill-use mode from ``react.skill_mode``."""
    if _retrieval_enabled(config):
        return SkillUseRail.SKILL_MODE_AUTO_LIST
    react = _config_section(config, "react")
    raw = react.get("skill_mode", SkillUseRail.SKILL_MODE_ALL)
    valid = {SkillUseRail.SKILL_MODE_AUTO_LIST, SkillUseRail.SKILL_MODE_ALL}
    return raw if isinstance(raw, str) and raw in valid else SkillUseRail.SKILL_MODE_ALL


def _retrieval_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether agentic skill retrieval is enabled for this config."""
    env_value = os.getenv("SYMPHONY_SKILL_RETRIEVAL_ENABLED")
    if env_value is not None and env_value.strip():
        return env_value.strip().lower() in {"1", "true", "yes", "on", "enabled"}

    symphony = _config_section(config or {}, "symphony")
    retrieval = symphony.get("skill_retrieval")
    if isinstance(retrieval, dict):
        return bool(retrieval.get("enabled", False))
    return False


def _team_skill_use_rail_spec(config: dict[str, Any], role: str) -> RailSpec:
    """Declare the team Skill rail for a member of either profile.

    Only the exposure mode and the seed allow-list are declared here. The rail
    reads the one physical Skill library narrowed by the member's and the team's
    ``skills-visibility.json``, and those declaration paths are keyed on a
    member identity that is minted per spawn, long after this spec is built —
    ``openjiuwen.agent_teams.skill.rail_spec.complete_declared_team_skill_rails``
    fills them in during member setup, together with the library root.

    Declaring the rail here rather than leaving it to the team's auto-add is
    what keeps ``react.skill_mode`` / agentic retrieval honoured: the auto-add
    has no config to read and would default to ALL mode.

    Args:
        config: The resolved config mapping.
        role: Member role, whose ``config.agents.<role>.skills`` seeds the
            member declaration the first time it is written.

    Returns:
        A ``core.team.skill_use`` RailSpec.
    """
    return RailSpec(
        type=TEAM_SKILL_USE,
        params={
            "skill_mode": _skill_mode(config),
            "bootstrap_allow": _resolve_member_skills(config, role),
        },
    )


def _collapse_skill_use_rails(
    rails: list[RailSpec], *, retrieval_enabled: bool
) -> list[RailSpec]:
    """Keep exactly one Skill rail, the first declared one.

    A member must never mount two Skill rails: they scan the same library and
    register the same ``skill`` / ``list_skill`` tools, which costs a duplicate
    prompt section and a duplicate-ability warning per tool on every build. The
    base spec's rails come first, so a blueprint that declares its own Skill
    rail wins over the one this module appends — the same precedence
    ``openjiuwen.agent_teams.skill.rail_spec`` applies on the team side.

    When agentic retrieval is on, the survivor is additionally pinned to
    auto-list: the model discovers Skills through the retrieval tools instead
    of having the whole library injected into its prompt.

    Args:
        rails: The merged rail declarations, base spec first.
        retrieval_enabled: Whether agentic skill retrieval is enabled.

    Returns:
        The rail list with at most one Skill rail left.
    """
    skill_rail_types = {CORE_SKILL_USE, TEAM_SKILL_USE, "skill_use", "SkillUseRail"}
    has_skill_rail = False
    collapsed: list[RailSpec] = []
    for rail in rails:
        if rail.type in skill_rail_types:
            if has_skill_rail:
                continue
            has_skill_rail = True
            if retrieval_enabled:
                params = dict(rail.params or {})
                params["skill_mode"] = SkillUseRail.SKILL_MODE_AUTO_LIST
                params["include_tools"] = False
                rail = rail.model_copy(update={"params": params})
            collapsed.append(rail)
            continue
        collapsed.append(rail)
    return collapsed


def _additional_directories(config: dict[str, Any]) -> list[str]:
    """Resolve project-memory additional directories from config + env."""
    react = _config_section(config, "react")
    raw = react.get("project_memory", {}).get("additional_directories")
    if raw is None:
        raw = os.getenv("JIUWENSWARM_ADDITIONAL_DIRECTORIES", "")
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _permission_model_name(config: dict[str, Any]) -> str:
    """Resolve the permission rail's model name from config."""
    return (
        _config_section(config, "models")
        .get("default", {})
        .get("model_client_config", {})
        .get("model_name", "gpt-4")
    )


def _acp_enabled(config: dict[str, Any]) -> bool:
    """Resolve whether ACP agents are configured (gates the acp_chat tool)."""
    acp_cfg = (config or {}).get("acp_agents")
    return isinstance(acp_cfg, dict) and bool(acp_cfg)


def _context_processor_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the context-compression rail.

    ``context_engine_config`` lives under ``react`` in config.yaml; reading it
    at the top level returned ``{}``, leaving the rail with an empty config and
    no compressors mounted.
    """
    react = _config_section(config, "react")
    return {
        "context_engine_enabled": get_context_engine_enabled(config),
        "context_engine_config": _config_section(react, "context_engine_config"),
    }


def _model_anomaly_detection_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the model-anomaly / tool-loop compact rail."""
    guard = _config_section(config, "execution_guard")
    return {
        "rail_config": _config_section(guard, "model_anomaly_detection_rail"),
    }


def _permission_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the permission-interrupt rail."""
    return {
        "permissions_config": _config_section(config, "permissions"),
        "model_name": _permission_model_name(config),
    }


def _team_evolution_rail_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the leader team skill-evolution rail."""
    return {
        "evolution_model_config": _evolution_model_config(config),
        "auto_save": get_evolution_auto_save_enabled(config),
    }


def _member_evolution_rail_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the member skill-evolution rail."""
    return {
        "evolution_model_config": _evolution_model_config(config),
    }


# Per-element attribute params, keyed by provider name; empty for parameterless.
_RAIL_PARAM_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    registry.CONTEXT_PROCESSOR: _context_processor_params,
    registry.MODEL_ANOMALY_DETECTION: _model_anomaly_detection_params,
    registry.CODE_PROJECT_MEMORY: lambda c: {
        "additional_directories": _additional_directories(c)
    },
    registry.PERMISSION_INTERRUPT: _permission_params,
    registry.TEAM_PERMISSION: lambda c: {
        "permissions_config": _config_section(c, "permissions"),
    },
    registry.TEAM_PERMISSION_POLICY: lambda c: {
        "permissions_config": _config_section(c, "permissions"),
    },
    registry.CODE_CODING_MEMORY: lambda c: {
        "embed_config": _config_section(c, "embed")
    },
    registry.USER_HOOKS: lambda c: {"hooks_section": _config_section(c, "hooks")},
    registry.CODE_WORKTREE: lambda c: {"enabled": True},
}


def _vision_tool_params(config: dict[str, Any]) -> dict[str, Any]:
    """Bake the core vision element's VisionModelConfig kwargs (empty disables it)."""
    return {"vision_model_config": _tools.vision_model_config_params(config)}


def _audio_tool_params(config: dict[str, Any]) -> dict[str, Any]:
    """Bake the core audio element's dedicated flag + AudioModelConfig kwargs."""
    return {
        "dedicated": _tools.audio_dedicated_configured(config),
        "audio_model_config": _tools.audio_model_config_params(config),
    }


_TOOL_PARAM_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    registry.SEND_FILE: lambda c: {"channels_config": _config_section(c, "channels")},
    registry.CODE_EXTRA_TOOLS: lambda c: {"acp_enabled": _acp_enabled(c)},
    registry.VISION: _vision_tool_params,
    registry.AUDIO: _audio_tool_params,
}


def _rail_params(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return attribute params baked into rail *name* (empty when parameterless)."""
    builder = _RAIL_PARAM_BUILDERS.get(name)
    return builder(config) if builder else {}


def _tool_params(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return attribute params baked into tool *name* (empty when parameterless)."""
    builder = _TOOL_PARAM_BUILDERS.get(name)
    return builder(config) if builder else {}


def _team_common_rail_names(role: str) -> tuple[str, ...]:
    """Shared chat-team rails; leaders omit harness todo planning."""
    if role == "leader":
        return tuple(
            name for name in _COMMON_RAIL_NAMES if name != registry.TASK_PLANNING
        )
    return _COMMON_RAIL_NAMES


def _code_base_rail_names(role: str) -> tuple[str, ...]:
    """Code-profile rails minus permission interrupt; leaders omit code todo planning.

    ``PERMISSION_INTERRUPT`` is excluded for all team members: it relies on a
    frontend user response that headless teammates cannot provide, and even the
    leader's interrupt path is unreliable in a team context.
    """
    names = tuple(
        name for name in _CODE_RAIL_NAMES if name != registry.PERMISSION_INTERRUPT
    )
    if role == "leader":
        return tuple(name for name in names if name != registry.CODE_TASK_PLANNING)
    return names


def _role_evolution_rails(config: dict[str, Any], role: str) -> list[RailSpec]:
    """Return the role-specific skill-evolution rails (shared by both profiles)."""
    if not get_skill_evolution_enabled(config):
        return []
    if role == "leader":
        return [
            RailSpec(
                type=registry.TEAM_SKILL_EVOLUTION,
                params=_team_evolution_rail_params(config),
            ),
            # No params: the Skill library and the team visibility metadata
            # path are per-team runtime values resolved from the build context.
            # The team name is not known at spec-build time, so neither path
            # can be baked in here.
            RailSpec(
                type=registry.TEAM_SKILL_CREATE,
                params={},
            ),
        ]
    return [
        RailSpec(
            type=registry.MEMBER_SKILL_EVOLUTION,
            params=_member_evolution_rail_params(config),
        ),
    ]


def _build_team_capability_specs(
    config: dict[str, Any],
    mode: str,
    role: str,
    *,
    enable_permissions: bool = False,
) -> tuple[list[RailSpec], list[BuiltinToolSpec]]:
    """Build the chat-team profile rail/tool specs for a member."""
    rails_specs: list[RailSpec] = [
        RailSpec(type=name, params=_rail_params(name, config))
        for name in _team_common_rail_names(role)
    ]
    if role == "leader":
        rails_specs.append(RailSpec(type=registry.STRUCTURED_ASK_USER))
        if is_team_plan_mode(mode):
            rails_specs.extend(
                [
                    RailSpec(type=registry.CODE_AGENT_MODE),
                    RailSpec(type=registry.TEAM_PLAN_APPROVAL),
                ]
            )

    rails_specs.append(_team_skill_use_rail_spec(config, role))
    rails_specs.append(RailSpec(type=registry.MEMBER_SKILL_TOOLKIT))

    if enable_permissions and role == "teammate":
        rails_specs.append(
            RailSpec(
                type=registry.TEAM_PERMISSION,
                params=_rail_params(registry.TEAM_PERMISSION, config),
            ),
        )

    if enable_permissions and role == "leader":
        rails_specs.append(
            RailSpec(
                type=registry.TEAM_PERMISSION_POLICY,
                params=_rail_params(registry.TEAM_PERMISSION_POLICY, config),
            ),
        )

    rails_specs.extend(_role_evolution_rails(config, role))

    tool_specs: list[BuiltinToolSpec] = [
        BuiltinToolSpec(type=name, params=_tool_params(name, config))
        for name in _COMMON_TOOL_NAMES
    ]
    return rails_specs, tool_specs


def _build_code_capability_specs(
    config: dict[str, Any],
    mode: str,
    role: str,
    *,
    enable_permissions: bool = False,
) -> tuple[list[RailSpec], list[BuiltinToolSpec]]:
    """Build the code profile (code.team / team.plan.code) specs for a member.

    PermissionInterruptRail (``swarm.permission_interrupt``) cannot resolve ASK
    interrupts on headless team members: the user-facing confirmation path
    requires a frontend connection that team members lack.  When
    ``enable_permissions`` is true the team permission rails replace it —
    ``TeamPermissionRail`` for teammates (leader-mediated ASK resolution) and
    ``TeamPermissionPolicyRail`` for the leader (prompt section injection).
    When ``enable_permissions`` is false the permission interrupt rail is
    removed entirely: it would deadlock a teammate on any ASK-level tool call.
    """
    is_team_plan_leader = is_team_plan_mode(mode) and role == "leader"

    rails_specs: list[RailSpec] = [
        RailSpec(type=name, params=_rail_params(name, config))
        for name in _code_base_rail_names(role)
    ]

    if is_team_plan_leader:
        rails_specs.append(RailSpec(type=registry.TEAM_PLAN_APPROVAL))

    if enable_permissions and role == "teammate":
        rails_specs.append(
            RailSpec(
                type=registry.TEAM_PERMISSION,
                params=_rail_params(registry.TEAM_PERMISSION, config),
            ),
        )

    if enable_permissions and role == "leader":
        rails_specs.append(
            RailSpec(
                type=registry.TEAM_PERMISSION_POLICY,
                params=_rail_params(registry.TEAM_PERMISSION_POLICY, config),
            ),
        )

    # The plan-only approval gate belongs to the Leader. A code Team Plan
    # teammate must otherwise be assembled exactly like a code.team teammate.
    if not is_team_plan_leader:
        rails_specs.append(
            RailSpec(
                type=registry.CODE_CONFIRM_INTERRUPT,
                params={"tool_names": ["switch_mode", "exit_plan_mode"]},
            ),
        )
    rails_specs.append(_team_skill_use_rail_spec(config, role))
    rails_specs.append(RailSpec(type=registry.MEMBER_SKILL_TOOLKIT))
    rails_specs.extend(
        RailSpec(type=name, params=_rail_params(name, config))
        for name in _CODE_SHARED_RAIL_NAMES
    )
    rails_specs.extend(_role_evolution_rails(config, role))

    tool_specs: list[BuiltinToolSpec] = [
        BuiltinToolSpec(type=name, params=_tool_params(name, config))
        for name in _CODE_TOOL_NAMES
    ]
    return rails_specs, tool_specs


def build_member_capability_specs(
    config: dict[str, Any],
    mode: str,
    role: str,
    *,
    enable_permissions: bool = False,
) -> tuple[list[RailSpec], list[BuiltinToolSpec]]:
    """Build the rail and tool specs for a team member.

    Branches by mode: the code modes get the code profile (``swarm.code_*``
    rails), all other modes get the chat-team profile. The member-skill toolkit
    rail carries the role's selected skill names, and the role adds its skill
    evolution rails.

    Args:
        config: The resolved ``config.yaml`` mapping (team blueprint shape).
        mode: The canonical team request mode.
        role: The member role ("leader" or "teammate").
        enable_permissions: Effective team permission toggle from TeamAgentSpec.

    Returns:
        A ``(rails_specs, tool_specs)`` tuple of openjiuwen specs.
    """
    if _is_code_mode(mode):
        return _build_code_capability_specs(
            config, mode, role, enable_permissions=enable_permissions
        )
    return _build_team_capability_specs(
        config, mode, role, enable_permissions=enable_permissions
    )


def _is_subagent_enabled(sub_cfg: Any) -> bool:
    """Return whether a ``react.subagents.<name>`` entry is enabled."""
    return isinstance(sub_cfg, dict) and bool(sub_cfg.get("enabled", False))


def _is_subagent_default_enabled(sub_cfg: Any) -> bool:
    """Return true unless a subagent is explicitly disabled."""
    return not isinstance(sub_cfg, dict) or sub_cfg.get("enabled", True) is not False


def _subagent_language(mode: str, role: str, config: dict[str, Any]) -> str:
    """Resolve the code sub-agent runtime language (mirrors ``code_runtime_language``).

    Code mode is English-only except the team.plan.code leader, which uses the
    configured preferred language. Baked into ``factory_kwargs`` so the generic
    openjiuwen sub-agent providers stay free of swarm mode/role policy.
    """
    if is_team_plan_mode(mode) and role == "leader":
        return resolve_language((config or {}).get("preferred_language", "zh"))
    return "en"


def _code_subagent_spec(
    name: str,
    factory_name: str,
    react_cfg: dict[str, Any],
    language: str,
) -> SubAgentSpec:
    """Build a declarative ``SubAgentSpec`` for a code sub-agent provider.

    The ``agent_card`` / ``system_prompt`` are placeholders: ``SubAgentSpec.build``
    short-circuits to the registered provider (by ``factory_name``), which returns
    a fully-formed ``SubAgentConfig``.
    """
    subagents_cfg = (
        react_cfg.get("subagents", {}) if isinstance(react_cfg, dict) else {}
    )
    sub_cfg = subagents_cfg.get(name) if isinstance(subagents_cfg, dict) else None
    if name == "browser_agent":
        max_iterations = DEFAULT_BROWSER_AGENT_MAX_ITERATIONS
    elif name == "statusline-setup":
        max_iterations = registry.DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS
    else:
        max_iterations = react_cfg.get(
            "max_iterations",
            _DEFAULT_SUBAGENT_MAX_ITERATIONS,
        )
    if isinstance(sub_cfg, dict) and sub_cfg.get("max_iterations"):
        max_iterations = sub_cfg["max_iterations"]
    card_kwargs: dict[str, Any] = {"name": name}
    if name == "statusline-setup":
        card_kwargs["id"] = "jiuwenswarm.statusline-setup"
    return SubAgentSpec(
        agent_card=AgentCard(**card_kwargs),
        system_prompt="",
        factory_name=factory_name,
        factory_kwargs={
            "max_iterations": int(max_iterations),
            "language": language,
        },
    )


def _is_explore_subagent(spec: Any) -> bool:
    """Return whether ``spec`` declares the explore sub-agent.

    Upstream team specs reach us in more than one shape, so all three identity
    carriers are checked: ``subagent_type`` (present on some platform spec
    objects), ``factory_name``, and the agent card name.

    Args:
        spec: A sub-agent spec from a base team spec or from
            :func:`build_member_subagent_specs`.

    Returns:
        True when the spec declares ``explore_agent``.
    """
    return (
        getattr(spec, "subagent_type", None) == "explore_agent"
        or getattr(spec, "factory_name", None) == registry.EXPLORE_AGENT
        or getattr(getattr(spec, "agent_card", None), "name", None) == "explore_agent"
    )


def build_member_subagent_specs(
    config: dict[str, Any],
    mode: str,
    role: str,
) -> list[SubAgentSpec]:
    """Build declarative member subagent specs.

    The status-line setup agent is available in every mode when enabled.
    Code modes additionally include explore / plan. Every sub-agent is gated by
    ``react.subagents.<name>.enabled``; status-line setup, explore and plan
    default to on (only an explicit ``false`` drops them), while code / browser
    require an explicit ``true``.

    Args:
        config: The resolved ``config.yaml`` mapping.
        mode: The request mode.
        role: The member role (reserved, both roles get the same sub-agents).

    Returns:
        The ``SubAgentSpec`` list. Non-code modes contain only the default-enabled
        status-line setup agent; code modes also contain their code sub-agents.
    """
    react = (config or {}).get("react", {})
    react = react if isinstance(react, dict) else {}
    subagents_cfg = react.get("subagents", {}) if isinstance(react, dict) else {}
    language = _subagent_language(mode, role, config)

    specs: list[SubAgentSpec] = []
    if _is_subagent_default_enabled(
        subagents_cfg.get("statusline-setup")
        if isinstance(subagents_cfg, dict)
        else None
    ):
        specs.append(
            _code_subagent_spec(
                "statusline-setup",
                registry.STATUSLINE_SETUP_AGENT,
                react,
                language,
            )
        )

    if not _is_code_mode(mode):
        return specs

# Explore / plan are the code profile's core sub-agents and stay mounted
    # unless a config entry turns them off explicitly, so an absent entry keeps
    # the long-standing behaviour.
    for name, factory_name in (
        ("explore_agent", registry.EXPLORE_AGENT),
        ("plan_agent", registry.PLAN_AGENT),
    ):
        sub_cfg = subagents_cfg.get(name) if isinstance(subagents_cfg, dict) else None
        if _is_subagent_default_enabled(sub_cfg):
            specs.append(_code_subagent_spec(name, factory_name, react, language))
    if isinstance(subagents_cfg, dict):
        if _is_subagent_enabled(subagents_cfg.get("code_agent")):
            specs.append(
                _code_subagent_spec("code_agent", registry.CODE_AGENT, react, language)
            )
        if _is_subagent_enabled(subagents_cfg.get("browser_agent")):
            specs.append(
                _code_subagent_spec(
                    "browser_agent", registry.SWARM_BROWSER_AGENT, react, language
                )
            )
    return specs


def build_member_deep_agent_spec(
    config: dict[str, Any],
    mode: str,
    role: str,
    base_spec: DeepAgentSpec,
    *,
    enable_permissions: bool = False,
    mcp_configs: list[McpServerConfig] | None = None,
) -> DeepAgentSpec:
    """Fold the member capability specs onto *base_spec*.

    Appends the rails/tools (and, for code modes, sub-agents + the code system
    prompt) after the base spec's existing entries and returns a new
    ``DeepAgentSpec`` (the input is left unmodified).

    Args:
        config: The resolved ``config.yaml`` mapping.
        mode: The canonical team request mode.
        role: The member role ("leader" or "teammate").
        base_spec: The base member ``DeepAgentSpec`` to extend.
        enable_permissions: Effective team permission toggle from TeamAgentSpec.
        mcp_configs: MCP server configs inherited from ``config.yaml``.

    Returns:
        A new ``DeepAgentSpec`` with the capability specs applied.
    """
    rails_specs, tool_specs = build_member_capability_specs(
        config,
        mode,
        role,
        enable_permissions=enable_permissions,
    )

    merged_rails = list(base_spec.rails or [])
    merged_rails.extend(rails_specs)
    merged_tools = list(base_spec.tools or [])
    merged_tools.extend(tool_specs)
    merged_mcps = _merge_mcp_configs(base_spec.mcps, mcp_configs)

    retrieval_enabled = _retrieval_enabled(config)
    merged_rails = _collapse_skill_use_rails(
        merged_rails, retrieval_enabled=retrieval_enabled
    )

    update: dict[str, Any] = {
        "rails": merged_rails,
        "tools": merged_tools,
        "mcps": merged_mcps,
        "kv_cache_affinity_config": _kv_cache_affinity_config(config),
    }
    if role == "leader":
        # Leaders use the team task board (create_task / view_task / update_task).
        # Force off agent-core's enable_task_planning auto-inject path so a YAML
        # base spec cannot re-mount harness todo rails via resolve_deep_agent_parts.
        update["enable_task_planning"] = False
    if not _is_code_mode(mode):
        update["enable_skill_discovery"] = not retrieval_enabled

    subagent_specs = build_member_subagent_specs(config, mode, role)

    # In team mode, base_spec includes a browser_agent with hardcoded
    # server_id="playwright_official_stdio". All members share that single
    # @playwright/mcp subprocess → single Chrome window. Replace it with
    # SWARM_BROWSER_AGENT which builds a unique server_id per member
    # (session_id + role), giving each member their own isolated Chrome.
    # In code mode build_member_subagent_specs already returns SWARM_BROWSER_AGENT,
    # so this branch only runs for non-code modes.
    team_browser_spec: SubAgentSpec | None = None
    if not _is_code_mode(mode):
        react_cfg = (config or {}).get("react", {})
        react_cfg = react_cfg if isinstance(react_cfg, dict) else {}
        subagents_cfg = (
            react_cfg.get("subagents", {}) if isinstance(react_cfg, dict) else {}
        )
        if isinstance(subagents_cfg, dict) and _is_subagent_enabled(
            subagents_cfg.get("browser_agent")
        ):
            language = _subagent_language(mode, role, config)
            team_browser_spec = _code_subagent_spec(
                "browser_agent", registry.SWARM_BROWSER_AGENT, react_cfg, language
            )

    if subagent_specs or team_browser_spec:
        merged_subagents = list(base_spec.subagents or [])
        # Drop a base_spec explore_agent when we contribute our own: code modes
        # always declare one in build_member_subagent_specs, and two specs under
        # the same name would just shadow each other in create_subagent.
        # SubAgentSpec has no subagent_type field, so the name / factory_name
        # checks are what actually match here.
        if any(_is_explore_subagent(spec) for spec in subagent_specs):
            merged_subagents = [
                spec for spec in merged_subagents if not _is_explore_subagent(spec)
            ]
        # Remove any browser_agent from base_spec to prevent the shared
        # playwright_official_stdio entry from co-existing with our isolated one.
        if team_browser_spec or any(
            getattr(s, "subagent_type", None) == "browser_agent" for s in subagent_specs
        ):
            merged_subagents = [
                s
                for s in merged_subagents
                if getattr(s, "subagent_type", None) != "browser_agent"
            ]
        if team_browser_spec:
            merged_subagents.append(team_browser_spec)
        merged_subagents.extend(subagent_specs)
        update["subagents"] = merged_subagents

    if _is_code_mode(mode):
        from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
            build_code_system_prompt,
        )

        update["system_prompt"] = build_code_system_prompt()

    return base_spec.model_copy(update=update)


def _merge_mcp_configs(
    base_mcps: list[McpServerConfig] | None,
    config_mcps: list[McpServerConfig] | None,
) -> list[McpServerConfig] | None:
    merged = list(base_mcps or [])
    if not config_mcps:
        return merged or None

    existing_ids = {str(getattr(cfg, "server_id", "") or "").strip() for cfg in merged}
    existing_names = {
        str(getattr(cfg, "server_name", "") or "").strip() for cfg in merged
    }

    for cfg in config_mcps:
        server_id = str(getattr(cfg, "server_id", "") or "").strip()
        server_name = str(getattr(cfg, "server_name", "") or "").strip()
        duplicate_id = bool(server_id and server_id in existing_ids)
        duplicate_name = bool(server_name and server_name in existing_names)
        if duplicate_id or duplicate_name:
            continue
        merged.append(cfg.model_copy(deep=True))
        if server_id:
            existing_ids.add(server_id)
        if server_name:
            existing_names.add(server_name)

    return merged or None


__all__ = [
    "build_member_capability_specs",
    "build_member_subagent_specs",
    "build_member_deep_agent_spec",
]
