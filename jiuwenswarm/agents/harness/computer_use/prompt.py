"""Append the bundled SDK Skill to the original CUA system prompt."""

import sys

from openjiuwen.harness.subagents.cua_agent import DEFAULT_CUA_AGENT_SYSTEM_PROMPT

from . import engine_root


def build_system_prompt(language: str, *, repl_enabled: bool = True) -> str:
    text = DEFAULT_CUA_AGENT_SYSTEM_PROMPT.get(language, DEFAULT_CUA_AGENT_SYSTEM_PROMPT["cn"])
    if sys.platform == "darwin":
        text += (
            "\nmacOS exception: get_window_state/zoom capture the window without activation. "
            "Single-step input uses a separate session defaulting to exclusive foreground input; "
            "an explicit COMPUTER_USE_DESKTOP is honored. Shared mode refuses that global-input route. "
            "Coordinates are Retina-normalized PNG pixels in the virtual screenshot canvas. "
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
        "\nFor simple observations or one explicit action, use the original tools. Prefer cua_repl "
        "for systematic multi-step workflows (多步工作流), loops and conditions. It executes Python, "
        "not JavaScript. Variables persist only within this delegated task; the host owns cleanup. "
        "Reads present text, printImage presents screenshots, and actions require explicit reads to verify. "
        "Never reuse old-tool references or coordinates in the REPL, or vice versa. REPL input follows "
        "its own host shared/exclusive policy. It runs with host permissions, not in a security sandbox.\n\n"
    ) + base.replace("computer_use({", "cua_repl({")
