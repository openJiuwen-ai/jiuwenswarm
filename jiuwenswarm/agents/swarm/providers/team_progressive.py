# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team-member ProgressiveToolRail: role-fixed eager lists, session-scoped meta tools.

Chat / code team members historically shipped the full registered tool schema on
every model call. Host ProgressiveToolRail only filters the adapter's own tools.
This provider mounts a per-member ``ProgressiveToolRail`` whose eager list is a
code constant (leader vs teammate), not ``react.tool_lazy_load.eager_tools``
(that yaml list serves single-agent and lacks team board verbs).

``agent_card_id`` is session-scoped so concurrent members do not share
``tools_search`` / ``invoke_tool`` resource_mgr entries or OfficeClaw pins.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ElementKind,
    harness_element,
)
from openjiuwen.harness.prompts import resolve_language

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    get_session_active_skill,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.config import (
    _get_ttse_config,
    coerce_config_bool,
    get_team_ttse_enabled,
    resolve_string_or_list_config,
)

logger = logging.getLogger(__name__)

# Provider name registered for the member progressive-tool rail.
TEAM_PROGRESSIVE_TOOL = "swarm.team_progressive_tool"

# Shared eager names for every team role. Order is schema order; do not reorder
# from ability_manager.list(). Meta tools stay first so mid-task swaps cannot
# break LLM prefix cache. verify_task / shutdown_member / todo_* stay deferred.
TEAM_SHARED_EAGER_TOOLS: tuple[str, ...] = (
    "tools_search",
    "invoke_tool",
    "send_message",
    "view_task",
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "glob",
    "list_files",
    "bash",
    "workspace_meta",
    "skill_tool",
    "memory_search",
    "memory_get",
    "powershell",
    "send_file_to_user",
    "web_search",
    "fetch_webpage",
)

# Leader-only board / HITL verbs (LEADER_TOOLS already omit claim_task).
TEAM_LEADER_EXTRA_EAGER_TOOLS: tuple[str, ...] = (
    "build_team",
    "create_task",
    "update_task",
    "ask_user",
)

# Teammate-only claim verb (MEMBER_TOOLS already omit create/update_task).
TEAM_TEAMMATE_EXTRA_EAGER_TOOLS: tuple[str, ...] = (
    "claim_task",
)

# code.team / team.plan only. These tools are gated by CodeConfirmInterruptRail
# and CodeAgentModeRail via ``before_tool_call`` on the outer tool name. If they
# sit in deferred and run through ``invoke_tool``, those rails only see
# ``invoke_tool`` and the confirm / plan-mode guards are skipped — same class of
# bypass as TeamPermissionRail before unwrap. Keep them eager (mirrors
# interface_code ``_CODE_EAGER_TOOLS`` mode trio + BUG20261105402443).
TEAM_CODE_MODE_EXTRA_EAGER_TOOLS: tuple[str, ...] = (
    "switch_mode",
    "enter_plan_mode",
    "exit_plan_mode",
)

_CODE_MEMBER_MODES = frozenset({"code.team", "team.plan"})

_TTSE_CONSULT_TOOL_NAME = "ttse_consult"
_TASK_TOOL_NAME = "task_tool"


def build_team_eager_tools(
    role: str | None,
    *,
    include_ttse: bool = False,
    include_task_tool: bool = False,
    include_code_mode_tools: bool = False,
) -> list[str]:
    """Return the ordered eager tool names for a team member role.

    Unregistered names are skipped later by ``ProgressiveToolRail`` filtering;
    this list never invents cards. Conditional names append after the role
    extras so the fixed prefix stays stable across TTSE / GP / code-mode toggles.
    """
    eager = list(TEAM_SHARED_EAGER_TOOLS)
    normalized = str(role or "").strip().lower()
    if normalized == "leader":
        eager.extend(TEAM_LEADER_EXTRA_EAGER_TOOLS)
    elif normalized == "teammate":
        eager.extend(TEAM_TEAMMATE_EXTRA_EAGER_TOOLS)
    if include_ttse and _TTSE_CONSULT_TOOL_NAME not in eager:
        eager.append(_TTSE_CONSULT_TOOL_NAME)
    if include_task_tool and _TASK_TOOL_NAME not in eager:
        eager.append(_TASK_TOOL_NAME)
    if include_code_mode_tools:
        for name in TEAM_CODE_MODE_EXTRA_EAGER_TOOLS:
            if name not in eager:
                eager.append(name)
    return eager


def _is_code_member_mode(mode: str | None) -> bool:
    return str(mode or "").strip().lower() in _CODE_MEMBER_MODES


def resolve_team_member_progressive_card_id(context: SwarmBuildContext) -> str:
    """Build a session-scoped meta-tool owner id for one team member.

    Host adapters use ``{card}_s_{session}``. Members share one chat session, so
    the card segment must include the member identity (``member_card_id`` /
    ``member_name``); otherwise concurrent members would collide on the same
    ``tools_search`` / ``invoke_tool`` resource_mgr entry.
    """
    session = str(getattr(context, "session_id", "") or "").strip()
    member_card = str(getattr(context, "member_card_id", "") or "").strip()
    member_name = str(getattr(context, "member_name", "") or "").strip()
    card = member_card or member_name or "member"
    if session:
        return f"{card}_s_{session}"
    # Non-empty so ProgressiveToolRail.init does not fall back to AgentCard.id.
    return f"{card}_s_team"


def _normalize_model_patterns(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _general_purpose_agent_enabled(react_config: dict[str, Any]) -> bool:
    subagents = react_config.get("subagents")
    if not isinstance(subagents, dict):
        return False
    gp = subagents.get("general_agent")
    return isinstance(gp, dict) and bool(gp.get("enabled", False))


def _team_ttse_consult_should_be_eager(config: dict[str, Any] | None) -> bool:
    """True when team TTSE is on and inject is on — mirrors single-agent inject gate.

    Uses ``react.ttse.team_enabled`` (not ``enabled``). The two switches are
    independent; mounting MEMBER_TTSE / TEAM_TTSE already keys off team_enabled.
    """
    if not get_team_ttse_enabled(config):
        return False
    return coerce_config_bool(_get_ttse_config(config).get("inject_enabled"), True)


def _react_section(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    react = config.get("react")
    return react if isinstance(react, dict) else {}


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_PROGRESSIVE_TOOL,
    description=(
        "Filters each team member's model tool schema to a role-fixed eager "
        "list; deferred tools stay reachable via tools_search + invoke_tool."
    ),
)
def _build_team_progressive_tool_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> ProgressiveToolRail:
    """Build ProgressiveToolRail for one team member from SwarmBuildContext.

    Reads ``react.tool_lazy_load.enabled`` / ``enable_for_models`` and TTSE /
    general-purpose gates. Does **not** read yaml ``eager_tools``.
    """
    del params
    full_config = getattr(context, "config", None)
    full_config = full_config if isinstance(full_config, dict) else {}
    react = _react_section(full_config)
    lazy_cfg = react.get("tool_lazy_load")
    if not isinstance(lazy_cfg, dict):
        lazy_cfg = {}
    lazy_load_enabled = bool(lazy_cfg.get("enabled", False))
    enable_for_models = _normalize_model_patterns(lazy_cfg.get("enable_for_models", []))

    include_ttse = _team_ttse_consult_should_be_eager(full_config)
    include_task_tool = _general_purpose_agent_enabled(react)
    include_code_mode_tools = _is_code_member_mode(getattr(context, "mode", None))
    eager_tools = build_team_eager_tools(
        getattr(context, "role", None),
        include_ttse=include_ttse,
        include_task_tool=include_task_tool,
        include_code_mode_tools=include_code_mode_tools,
    )

    language = resolve_language(getattr(context, "language", None) or "cn")
    card_id = resolve_team_member_progressive_card_id(context)
    session_id = str(getattr(context, "session_id", "") or "").strip()

    disabled_tools: list[str] = []
    try:
        disabled_tools = resolve_string_or_list_config(react.get("disabled_tools"))
    except Exception as exc:  # noqa: BLE001 - malformed config must not block mount
        logger.warning(
            "[TeamProgressiveTool] ignore malformed disabled_tools: %s",
            exc,
        )

    def _active_skill_provider() -> str | None:
        # Prefer the member-scoped owner key so concurrent members cannot share
        # an active-skill unlock. Fall back to the chat session id when a host
        # SkillActiveStateRail wrote there (members do not mount that rail yet).
        skill = get_session_active_skill(card_id)
        if skill:
            return skill
        if session_id:
            return get_session_active_skill(session_id)
        return None

    logger.info(
        "[TeamProgressiveTool] mounted lazy_load_enabled=%s role=%s "
        "member=%s eager_tools=%s agent_card_id=%s enable_for_models=%s",
        lazy_load_enabled,
        getattr(context, "role", None) or "?",
        getattr(context, "member_name", None) or "?",
        eager_tools,
        card_id,
        enable_for_models,
    )
    return ProgressiveToolRail(
        enabled=lazy_load_enabled,
        eager_tools=eager_tools,
        language=language,
        agent_id=card_id,
        agent_card_id=card_id,
        enable_for_models=enable_for_models,
        active_skill_provider=_active_skill_provider,
        disabled_tools=disabled_tools,
    )


__all__ = [
    "TEAM_PROGRESSIVE_TOOL",
    "TEAM_SHARED_EAGER_TOOLS",
    "TEAM_LEADER_EXTRA_EAGER_TOOLS",
    "TEAM_TEAMMATE_EXTRA_EAGER_TOOLS",
    "TEAM_CODE_MODE_EXTRA_EAGER_TOOLS",
    "build_team_eager_tools",
    "resolve_team_member_progressive_card_id",
    "_team_ttse_consult_should_be_eager",
    "_is_code_member_mode",
]
