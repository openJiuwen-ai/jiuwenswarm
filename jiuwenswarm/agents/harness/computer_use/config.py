"""Extend the original CUA subagent through its existing rail hook."""

from __future__ import annotations

from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.subagents.cua_agent import build_cua_agent_config as build_core_cua_config

from .prompt import build_system_prompt
from .rail import CuaExtensionRail


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
    cua_repl_enabled=True,
    cua_repl_python=None,
):
    """Keep the core CUA factory, tools, capability policy and default rails."""
    spec = build_core_cua_config(
        model,
        card=card,
        system_prompt=build_system_prompt(language, repl_enabled=cua_repl_enabled),
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        max_iterations=max_iterations,
        cua_snapshot_keep_last_k=cua_snapshot_keep_last_k,
        cua_pause_on_user_input=cua_pause_on_user_input,
        rails=[CuaExtensionRail(repl_enabled=cua_repl_enabled, python=cua_repl_python,
                                pause_on_user_input=cua_pause_on_user_input)],
    )
    spec.parallel_tool_calls = False
    return spec
