"""Shared jiuwenswarm defaults and config parsing for the cua (desktop) sub-agent."""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.image_modality_probe import get_cached_image_support

logger = logging.getLogger(__name__)

# Raised above agent-core's ``create_cua_agent`` default (25): a perceive-act-verify loop
# re-captures the window after every action, so tighter budgets run dry mid-task.
DEFAULT_CUA_AGENT_MAX_ITERATIONS = 100

# agent-core default: window screenshots kept in full in the sub-agent context.
# 3 suits two-window tasks; 1 halves the context for single-window tasks.
DEFAULT_CUA_SNAPSHOT_KEEP_LAST_K = 3

# Replaces agent-core's one-line card description. The parent's task-tool
# prompt tells the model to skip delegation for tasks "not related to the
# subagent descriptions", so the card has to name the kinds of tasks it takes.
CUA_AGENT_CARD_DESCRIPTION = {
    "cn": (
        "专用桌面子代理，通过截图、无障碍树和鼠标键盘操作本机应用窗口；"
        "简单观察可用单步工具，多步工作流优先通过 Python cua_repl 连续执行。支持启动应用、"
        "在原生窗口（Office、IDE、聊天客户端、系统对话框等）内点击/输入/按快捷键/滚动、"
        "读取窗口当前内容、查看已打开的应用与窗口。凡是要在本机图形界面上执行的操作，"
        "都应交给它，而不是自行用脚本驱动界面。"
    ),
    "en": (
        "Dedicated desktop subagent that controls host application windows through "
        "screenshots, accessibility and mouse/keyboard input. Use individual tools for simple "
        "observations; prefer Python cua_repl for multi-step workflows. Supports launching apps, "
        "clicking / typing / pressing hotkeys / "
        "scrolling inside native windows (Office, IDEs, chat clients, system dialogs), "
        "reading what a window currently shows, and listing open apps and windows. "
        "Use it for any action on this machine's GUI instead of scripting the UI yourself."
    ),
}


def build_cua_agent_card(language: str = "cn") -> AgentCard:
    """Return the ``cua_agent`` card with the task-oriented description."""

    return AgentCard(
        name="cua_agent",
        description=CUA_AGENT_CARD_DESCRIPTION.get(
            language, CUA_AGENT_CARD_DESCRIPTION["cn"]
        ),
    )


def cua_model_rejects_images(model: Any) -> bool:
    """Return True only when ``model`` is known to reject image input.

    cua_agent needs screenshot perception alongside accessibility, so on such a
    model every call after the first capture fails and the run burns its whole
    iteration budget. The verdict comes from the image-modality probe cache
    (warmed for every ``models.defaults`` entry at server start, or declared via
    ``supports_vision``); an unknown verdict keeps the agent mounted.
    """
    return get_cached_image_support(model) is False


def resolve_cua_factory_options(sub_cfg: Any) -> dict[str, Any]:
    """Map ``react.subagents.cua_agent`` knobs onto ``build_cua_agent_config`` kwargs.

    agent-core raises ``ValueError`` at config time on a bad value, which would
    abort the whole agent build; invalid entries are therefore dropped here
    with a warning and the agent-core default applies.
    """
    cfg = sub_cfg if isinstance(sub_cfg, dict) else {}
    options: dict[str, Any] = {}

    raw_repl = cfg.get("repl_enabled")
    if isinstance(raw_repl, bool):
        options["cua_repl_enabled"] = raw_repl
    elif raw_repl is not None:
        logger.warning("[cua_agent] ignoring repl_enabled=%r (need true/false)", raw_repl)
    raw_python = cfg.get("repl_python")
    if isinstance(raw_python, str) and raw_python.strip():
        options["cua_repl_python"] = raw_python.strip()
    elif raw_python is not None:
        logger.warning("[cua_agent] ignoring repl_python (need a Python executable)")

    raw_k = cfg.get("snapshot_keep_last_k")
    if raw_k is not None:
        keep = None
        if isinstance(raw_k, int) and not isinstance(raw_k, bool):
            keep = raw_k
        elif isinstance(raw_k, str) and raw_k.strip().isdigit():
            keep = int(raw_k.strip())
        if keep is not None and keep >= 1:
            options["cua_snapshot_keep_last_k"] = keep
        else:
            logger.warning(
                "[cua_agent] ignoring snapshot_keep_last_k=%r (need an int >= 1); using %s",
                raw_k,
                DEFAULT_CUA_SNAPSHOT_KEEP_LAST_K,
            )

    raw_pause = cfg.get("pause_on_user_input")
    if raw_pause is not None:
        if isinstance(raw_pause, bool):
            options["cua_pause_on_user_input"] = raw_pause
        elif isinstance(raw_pause, str) and raw_pause.strip().lower() in (
            "true",
            "false",
        ):
            options["cua_pause_on_user_input"] = raw_pause.strip().lower() == "true"
        else:
            logger.warning(
                "[cua_agent] ignoring pause_on_user_input=%r (need true/false); keeping the default (off)",
                raw_pause,
            )

    return options


__all__ = [
    "CUA_AGENT_CARD_DESCRIPTION",
    "DEFAULT_CUA_AGENT_MAX_ITERATIONS",
    "DEFAULT_CUA_SNAPSHOT_KEEP_LAST_K",
    "build_cua_agent_card",
    "cua_model_rejects_images",
    "resolve_cua_factory_options",
]
