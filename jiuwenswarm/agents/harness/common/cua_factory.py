"""Mount the external REPL on the existing desktop subagent."""

from __future__ import annotations

from importlib.resources import files
import sys

from openjiuwen.harness.subagents.cua_agent import build_cua_agent_config as build_core_cua_config


def build_cua_agent_config(
    model,
    *,
    card=None,
    workspace=None,
    sys_operation=None,
    language="cn",
    max_iterations=100,
    cua_snapshot_keep_last_k=3,
    cua_pause_on_user_input=False,
    rails=None,
):
    """Preserve the core factory for source and wheel installations."""
    spec = build_core_cua_config(
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
    if not getattr(sys, "frozen", False) or sys.platform not in ("darwin", "win32"):
        return spec

    from .rails.cua_extension_rail import CuaExtensionRail

    spec.rails = [*(spec.rails or []), CuaExtensionRail(pause_on_user_input=cua_pause_on_user_input)]
    api = files("computer_use").joinpath("__init__.pyi").read_text(encoding="utf-8")
    spec.system_prompt += (
        "\nUse the single-step tools for a simple observation or action. Prefer cua_repl for "
        "multi-step workflows, loops and conditions. It executes Python with top-level await; "
        "computer, print and print_image are preloaded. Await SDK operations sequentially. "
        "Variables persist within this delegation; the host closes the session afterwards. "
        "Reads present text automatically; use print_image for images. Read again to verify actions. "
        "Keep its references and screenshot coordinates separate from the single-step tools. "
        "After a timeout or cancellation, actions may already have executed; never replay them "
        "automatically. This code runs with the host user's permissions.\n"
        f"Public Computer Use SDK interface:\n```python\n{api}\n```"
    )
    if sys.platform == "darwin":
        spec.system_prompt += (
            "\nOn macOS, use cua_repl for desktop operations; "
            "the legacy single-step backend is unavailable.\n"
        )
    spec.parallel_tool_calls = False
    return spec
