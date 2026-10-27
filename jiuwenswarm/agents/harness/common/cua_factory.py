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
    rails=None,
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
            rails=rails,
        )

    from .rails.cua_extension_rail import CuaExtensionRail

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
        rails=[*(rails or []), CuaExtensionRail(pause_on_user_input=cua_pause_on_user_input)],
    )
    spec.parallel_tool_calls = False
    return spec


def build_system_prompt(language: str, *, repl_enabled: bool = True) -> str:
    """Append the bundled SDK Skill to the original CUA system prompt."""
    from openjiuwen.harness.subagents.cua_agent import DEFAULT_CUA_AGENT_SYSTEM_PROMPT

    from jiuwenswarm.cua_engine.hosting import engine_root

    text = DEFAULT_CUA_AGENT_SYSTEM_PROMPT.get(language, DEFAULT_CUA_AGENT_SYSTEM_PROMPT["cn"])
    if sys.platform == "darwin":
        text += (
            "\nOn macOS, get_window_state and zoom observe the window without activating it. "
            "Use cmd/super for Command.\n"
        )
    if not repl_enabled:
        return text
    platform_skill = {"darwin": "computer-use-macos", "win32": "computer-use-windows"}.get(sys.platform)
    skill = (engine_root() / "skills" / platform_skill / "SKILL.md" if platform_skill
             else engine_root() / "skills/computer-use/base.md")
    base = skill.read_text(encoding="utf-8")
    if base.startswith("---\n"):
        base = base.split("---", 2)[2].lstrip()
    before, cleanup, after = base.partition("## Session cleanup")
    if cleanup:
        _, api, remainder = after.partition("## API surface")
        if not api:
            raise ValueError("The bundled Computer Use Skill has no API surface")
        base = before + api + remainder
    return text + (
        "\nUse the single-step tools for a simple observation or action. Prefer cua_repl "
        "for multi-step workflows (多步工作流), loops and conditions. Keep its element references "
        "and screenshot coordinates separate from the single-step tools.\n\n"
    ) + base.replace("computer_use({", "cua_repl({")
