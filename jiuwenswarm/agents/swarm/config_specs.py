# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-sourced capability specs for swarm team members.

This module is the rewritten "member capability" assembly: a member's rails,
tools and sub-agents are declared entirely as ``RailSpec`` / ``BuiltinToolSpec``
/ ``SubAgentSpec`` references to ``swarm.*`` providers, derived from the config
source and the member role. It deliberately depends on **no DeepAgent instance**
— only on the config mapping and the provider name constants re-exported by
:mod:`jiuwenswarm.agents.swarm.registry`.

``build_member_deep_agent_spec`` folds those specs onto a base ``DeepAgentSpec``
so openjiuwen builds the member from the merged spec. Two modes are supported:

* ``team`` — the chat team profile (common rails + base tools).
* ``code.team`` / ``team.plan`` — the code profile (``swarm.code_*`` rails, code
    sub-agents, code system prompt), all still purely declarative.
"""

from __future__ import annotations

import logging
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
from openjiuwen.core.foundation.kv_cache import KVCacheAffinityConfig
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.common.config import (
    ASCEND_AFFINITY_PROVIDER,
    get_default_model_provider,
    get_evolution_auto_save_enabled,
    get_evolution_review_trigger_enabled,
    get_evolution_signal_trigger_enabled,
    get_passive_skill_evolution_triggers,
    get_skill_create_enabled,
    resolve_string_or_list_config,
)
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    get_context_engine_enabled,
    resolve_model_config,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.providers import tools as _tools

# Modes that route to the code adapter and get the code member profile.
_CODE_MODES: frozenset[str] = frozenset({"code.team", "team.plan"})
logger = logging.getLogger(__name__)


def _kv_cache_affinity_config(config: dict[str, Any]) -> KVCacheAffinityConfig:
    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    raw = react.get("kv_cache_affinity_config")
    raw = raw if isinstance(raw, dict) else {}
    affinity_enabled = bool(raw.get("enable_kv_cache_affinity", False))
    if affinity_enabled:
        provider = get_default_model_provider(config)
        if provider != ASCEND_AFFINITY_PROVIDER:
            logger.warning(
                "Team KV cache affinity failed closed: default provider=%s requires=%s",
                provider or "<empty>",
                ASCEND_AFFINITY_PROVIDER,
            )
            affinity_enabled = False
    return KVCacheAffinityConfig(
        enable_kv_cache_release=bool(raw.get("enable_kv_cache_release", False)),
        enable_kv_cache_affinity=affinity_enabled,
    )


# Rails common to both roles, in mount order. Each entry is a ``swarm.*``
# provider name re-exported from the registry (no hard-coded strings).
_COMMON_RAIL_NAMES: tuple[str, ...] = (
    registry.RUNTIME_PROMPT,
    registry.TEAM_SKILL_STORAGE_POLICY,
    registry.TEAM_SHARED_SKILL_LINK_REFRESH,
    registry.RESPONSE_PROMPT,
    registry.SYS_OPERATION,
    registry.STREAM_EVENT,
    registry.TASK_PLANNING,
    registry.SECURITY,
    registry.HEARTBEAT,
    registry.AVATAR_PROMPT,
    registry.TEAM_WORKSPACE_REPORT_PATH,
    registry.CONTEXT_PROCESSOR,
    registry.PLUGIN_RAILS,
    registry.SKILL_RETRIEVAL_PROMPT,
    registry.SYMPHONY_ORCHESTRATION_PROMPT,
    registry.A2A_OUTBOUND_TOOLKIT,
    registry.DISABLED_TOOLS,
)

# Tools common to both roles. Each element self-gates on config, so all are
# declared; an unconfigured element simply yields no tools.
_COMMON_TOOL_NAMES: tuple[str, ...] = (
    registry.WEB_SEARCH,
    registry.WEB_FETCH,
    registry.VISION,
    registry.AUDIO,
    # Skill-management tools (search/install/uninstall_skill) are registered by
    # the MEMBER_SKILL_TOOLKIT rail (appended below), which owns them with a
    # link-refresh callback. Declaring SKILL_TOOLKIT here too only double-
    # registers the same-named tools, logging a refresh + duplicate-ability
    # warning per tool every build; the rail is the sole registrar.
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
# code-specific rails). ``code_confirm_interrupt`` and ``member_skill_toolkit``
# carry params and are appended separately.
_CODE_RAIL_NAMES: tuple[str, ...] = (
    registry.CODE_RUNTIME_PROMPT,
    registry.RESPONSE_PROMPT,
    registry.STREAM_EVENT,
    registry.SECURITY,
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
    registry.CODE_SKILL_USE,
    registry.SKILL_RETRIEVAL_PROMPT,
    registry.SYMPHONY_ORCHESTRATION_PROMPT,
    registry.A2A_OUTBOUND_TOOLKIT,
    registry.DISABLED_TOOLS,
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
    """Resolve the skill names selected for *role* from the config source.

    Mirrors the legacy ``resolve_member_skills``: reads ``config.agents.<role>.skills``
    and returns the cleaned, non-empty names.

    Args:
        config: The resolved ``config.yaml`` mapping (team blueprint shape).
        role: The member role ("leader" or "teammate").

    Returns:
        The selected skill names for the role (possibly empty).
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


def _normalize_skill_use_rails_for_agentic_retrieval(rails: list[RailSpec]) -> list[RailSpec]:
    """Normalize skill-use rails to auto-list mode and remove duplicates."""
    skill_rail_types = {CORE_SKILL_USE, "skill_use", "SkillUseRail", registry.CODE_SKILL_USE}
    has_skill_rail = False
    normalized: list[RailSpec] = []
    for rail in rails:
        if rail.type in skill_rail_types:
            if has_skill_rail:
                continue
            has_skill_rail = True
            params = dict(rail.params or {})
            params["skill_mode"] = SkillUseRail.SKILL_MODE_AUTO_LIST
            params["include_tools"] = False
            normalized.append(rail.model_copy(update={"params": params}))
            continue
        normalized.append(rail)
    return normalized


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
        "review_trigger": get_evolution_review_trigger_enabled(config),
        "auto_save": get_evolution_auto_save_enabled(config),
    }


def _member_evolution_rail_params(config: dict[str, Any]) -> dict[str, Any]:
    """Attribute params for the member skill-evolution rail."""
    triggers = get_passive_skill_evolution_triggers(config)
    return {
        "evolution_model_config": _evolution_model_config(config),
        "signal_trigger": triggers["signal_trigger"],
        "review_trigger": triggers["review_trigger"],
    }


# Per-element attribute params, keyed by provider name; empty for parameterless.
_RAIL_PARAM_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    registry.DISABLED_TOOLS: lambda c: {
        "disabled_tools": resolve_string_or_list_config(
            _config_section(c, "react").get("disabled_tools")
        )
    },
    registry.CONTEXT_PROCESSOR: _context_processor_params,
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
    registry.CODE_SKILL_USE: lambda c: {
        "skill_mode": _skill_mode(c),
        "include_tools": not _retrieval_enabled(c),
    },
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
        return tuple(name for name in _COMMON_RAIL_NAMES if name != registry.TASK_PLANNING)
    return _COMMON_RAIL_NAMES


def _code_base_rail_names(role: str) -> tuple[str, ...]:
    """Code-profile rails minus permission interrupt; leaders omit code todo planning.

    ``PERMISSION_INTERRUPT`` remains excluded from code.team/team.plan for all
    members: teammates are headless, and these profiles retain their historical
    Leader-mediated approval path.  Ordinary chat-team Leaders are assembled by
    ``_build_team_capability_specs`` and do receive the user-facing interrupt.
    """
    names = tuple(
        name for name in _CODE_RAIL_NAMES
        if name != registry.PERMISSION_INTERRUPT
    )
    if role == "leader":
        return tuple(name for name in names if name != registry.CODE_TASK_PLANNING)
    return names


def _role_evolution_rails(config: dict[str, Any], role: str) -> list[RailSpec]:
    """Return the role-specific skill-evolution rails (shared by both profiles)."""
    if role == "leader":
        return [
            RailSpec(
                type=registry.TEAM_SKILL_EVOLUTION,
                params=_team_evolution_rail_params(config),
            ),
            RailSpec(
                type=registry.TEAM_SKILL_CREATE,
                params={"skill_create": get_skill_create_enabled(config)},
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

    if _retrieval_enabled(config):
        rails_specs.append(
            RailSpec(
                type=CORE_SKILL_USE,
                params={
                    "skill_mode": SkillUseRail.SKILL_MODE_AUTO_LIST,
                    "include_tools": False,
                },
            )
        )
    rails_specs.append(
        RailSpec(
            type=registry.MEMBER_SKILL_TOOLKIT,
            params={"skills": _resolve_member_skills(config, role)},
        )
    )

    if enable_permissions and role == "teammate":
        # Teammates are headless in a Team run.  TeamPermissionRail forwards
        # ASK decisions to the Leader's approve_tool flow instead of creating
        # a frontend interrupt for the teammate itself.
        rails_specs.append(
            RailSpec(
                type=registry.TEAM_PERMISSION,
                params=_rail_params(registry.TEAM_PERMISSION, config),
            ),
        )

    if enable_permissions and role == "leader":
        # These Rails serve different consumers: the policy Rail teaches the
        # Leader how to approve teammate requests, while PermissionInterruptRail
        # exposes the Leader's own ASK decisions through the user-facing Team
        # stream.  Keeping both mounted avoids silently auto-approving Leader
        # tool calls and does not change teammate approval semantics.
        rails_specs.extend(
            [
                RailSpec(
                    type=registry.TEAM_PERMISSION_POLICY,
                    params=_rail_params(registry.TEAM_PERMISSION_POLICY, config),
                ),
                RailSpec(
                    type=registry.PERMISSION_INTERRUPT,
                    params=_rail_params(registry.PERMISSION_INTERRUPT, config),
                ),
            ]
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
    """Build the code profile (code.team / team.plan) rail/tool specs for a member.

    PermissionInterruptRail (``swarm.permission_interrupt``) cannot resolve ASK
    interrupts on headless team members: the user-facing confirmation path
    requires a frontend connection that team members lack.  When
    ``enable_permissions`` is true the team permission rails replace it —
    ``TeamPermissionRail`` for teammates (leader-mediated ASK resolution) and
    ``TeamPermissionPolicyRail`` for the leader (prompt section injection).
    When ``enable_permissions`` is false the permission interrupt rail is
    removed entirely: it would deadlock a teammate on any ASK-level tool call.
    """
    is_team_plan_leader = mode == "team.plan" and role == "leader"

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

    if mode != "team.plan":
        rails_specs.append(
            RailSpec(
                type=registry.CODE_CONFIRM_INTERRUPT,
                params={"tool_names": ["switch_mode", "exit_plan_mode"]},
            ),
        )
    rails_specs.append(
        RailSpec(
            type=registry.MEMBER_SKILL_TOOLKIT,
            params={"skills": _resolve_member_skills(config, role)},
        )
    )
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
        mode: The request mode ("team" / "code.team" / "team.plan").
        role: The member role ("leader" or "teammate").
        enable_permissions: Effective team permission toggle from TeamAgentSpec.

    Returns:
        A ``(rails_specs, tool_specs)`` tuple of openjiuwen specs.
    """
    if _is_code_mode(mode):
        return _build_code_capability_specs(config, mode, role, enable_permissions=enable_permissions)
    return _build_team_capability_specs(config, role, enable_permissions=enable_permissions)


def _is_subagent_enabled(sub_cfg: Any) -> bool:
    """Return whether a ``react.subagents.<name>`` entry is enabled."""
    return isinstance(sub_cfg, dict) and bool(sub_cfg.get("enabled", False))


def _subagent_language(mode: str, role: str, config: dict[str, Any]) -> str:
    """Resolve the code sub-agent runtime language (mirrors ``code_runtime_language``).

    Code mode is English-only except the team.plan leader, which uses the
    configured preferred language. Baked into ``factory_kwargs`` so the generic
    openjiuwen sub-agent providers stay free of swarm mode/role policy.
    """
    if mode == "team.plan" and role == "leader":
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
    max_iterations = react_cfg.get("max_iterations", _DEFAULT_SUBAGENT_MAX_ITERATIONS)
    if isinstance(sub_cfg, dict) and sub_cfg.get("max_iterations"):
        max_iterations = sub_cfg["max_iterations"]
    return SubAgentSpec(
        agent_card=AgentCard(name=name),
        system_prompt="",
        factory_name=factory_name,
        factory_kwargs={
            "max_iterations": int(max_iterations),
            "language": language,
        },
    )


def build_member_subagent_specs(
    config: dict[str, Any],
    mode: str,
    role: str,
) -> list[SubAgentSpec]:
    """Build the declarative code sub-agent specs (empty for non-code modes).

    explore / plan are always present, while code / browser are config-gated via
    ``react.subagents.<name>.enabled``.

    Args:
        config: The resolved ``config.yaml`` mapping.
        mode: The request mode.
        role: The member role (reserved, both roles get the same sub-agents).

    Returns:
        The ``SubAgentSpec`` list (empty when not a code mode).
    """
    if not _is_code_mode(mode):
        return []
    react = (config or {}).get("react", {})
    react = react if isinstance(react, dict) else {}
    subagents_cfg = react.get("subagents", {}) if isinstance(react, dict) else {}
    language = _subagent_language(mode, role, config)

    specs: list[SubAgentSpec] = [
        _code_subagent_spec("explore_agent", registry.EXPLORE_AGENT, react, language),
        _code_subagent_spec("plan_agent", registry.PLAN_AGENT, react, language),
    ]
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
        mode: The request mode ("team" / "code.team" / "team.plan").
        role: The member role ("leader" or "teammate").
        base_spec: The base member ``DeepAgentSpec`` to extend.
        enable_permissions: Effective team permission toggle from TeamAgentSpec.
        mcp_configs: MCP server configs inherited from ``config.yaml``.

    Returns:
        A new ``DeepAgentSpec`` with the capability specs applied.
    """
    rails_specs, tool_specs = build_member_capability_specs(
        config, mode, role, enable_permissions=enable_permissions,
    )

    merged_rails = list(base_spec.rails or [])
    merged_rails.extend(rails_specs)
    merged_tools = list(base_spec.tools or [])
    merged_tools.extend(tool_specs)
    merged_mcps = _merge_mcp_configs(base_spec.mcps, mcp_configs)

    retrieval_enabled = _retrieval_enabled(config)
    if retrieval_enabled:
        merged_rails = _normalize_skill_use_rails_for_agentic_retrieval(merged_rails)

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
        subagents_cfg = react_cfg.get("subagents", {}) if isinstance(react_cfg, dict) else {}
        if isinstance(subagents_cfg, dict) and _is_subagent_enabled(subagents_cfg.get("browser_agent")):
            language = _subagent_language(mode, role, config)
            team_browser_spec = _code_subagent_spec(
                "browser_agent", registry.SWARM_BROWSER_AGENT, react_cfg, language
            )

    if subagent_specs or team_browser_spec:
        merged_subagents = list(base_spec.subagents or [])
        # Remove any browser_agent from base_spec to prevent the shared
        # playwright_official_stdio entry from co-existing with our isolated one.
        if team_browser_spec or any(
            getattr(s, "subagent_type", None) == "browser_agent" for s in subagent_specs
        ):
            merged_subagents = [
                s for s in merged_subagents
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

    existing_ids = {
        str(getattr(cfg, "server_id", "") or "").strip()
        for cfg in merged
    }
    existing_names = {
        str(getattr(cfg, "server_name", "") or "").strip()
        for cfg in merged
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
