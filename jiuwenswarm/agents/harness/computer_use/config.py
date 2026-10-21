"""Extend the original CUA subagent through its existing rail hook."""

from __future__ import annotations

import sys

from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.subagents.cua_agent import build_cua_agent_config as build_core_cua_config


def build_cua_agent_config(
    model,
    *,
    card: AgentCard,
    workspace=None,
    sys_operation=None,
    language="cn",
    max_iterations=100,
    cua_snapshot_keep_last_k=3,
    cua_pause_on_user_input=False,
):
    """Extend desktop installers; preserve the core factory for Python installs."""
    if not getattr(sys, "frozen", False) or sys.platform not in ("darwin", "win32"):
        return build_core_cua_config(
            model,
            card=card,
            workspace=workspace,
            sys_operation=sys_operation,
            language=language,
            max_iterations=max_iterations,
            cua_snapshot_keep_last_k=cua_snapshot_keep_last_k,
            cua_pause_on_user_input=cua_pause_on_user_input,
        )

    from .prompt import build_system_prompt
    from .rail import CuaExtensionRail

    spec = build_core_cua_config(
        model,
        card=card,
        system_prompt=build_system_prompt(language),
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        max_iterations=max_iterations,
        cua_snapshot_keep_last_k=cua_snapshot_keep_last_k,
        cua_pause_on_user_input=cua_pause_on_user_input,
        rails=[CuaExtensionRail(pause_on_user_input=cua_pause_on_user_input)],
    )
    spec.parallel_tool_calls = False
    return spec
