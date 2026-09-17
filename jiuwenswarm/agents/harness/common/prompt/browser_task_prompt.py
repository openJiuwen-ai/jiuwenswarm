# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser guidance appended to agent-core's ``task_tool`` section."""

from __future__ import annotations


_BROWSER_TASK_PROMPT = {
    "cn": (
        "## 浏览器子智能体规则\n\n"
        "- 打开页面、跳转、点击、输入、登录、截图、页面检查或从实时网站提取数据时，"
        "使用 `task_tool`，将 `subagent_type` 设为 `\"browser_agent\"`，并在 "
        "`task_description` 中写明完整目标。\n"
        "- 不要通过 Bash、代码执行、子进程、Shell 命令或直接启动 Chrome/Edge "
        "来执行浏览器自动化。\n"
        "- `task_tool` 或 `browser_agent` 不可用时，明确说明不可用，不要改用命令行启动浏览器。"
    ),
    "en": (
        "## Browser Subagent Rules\n\n"
        "- When opening pages, navigating, clicking, typing, logging in, taking screenshots, "
        "inspecting pages, or extracting data from live websites, use `task_tool`, set "
        '`subagent_type` to `"browser_agent"`, and state the complete objective in '
        "`task_description`.\n"
        "- Do not use Bash, code execution, subprocesses, shell commands, or direct Chrome/Edge launches "
        "for browser automation.\n"
        "- If `task_tool` or `browser_agent` is unavailable, state that clearly; do not fall back "
        "to launching a browser from the command line."
    ),
}


def build_browser_task_prompt(language: str = "cn") -> str:
    """Return localized browser guidance for the ``task_tool`` section."""

    return _BROWSER_TASK_PROMPT.get(language, _BROWSER_TASK_PROMPT["cn"])


__all__ = ["build_browser_task_prompt"]
