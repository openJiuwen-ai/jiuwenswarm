# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser guidance appended to agent-core's subagent tool section."""

from __future__ import annotations


_BROWSER_TASK_PROMPT = {
    "cn": (
        "## 浏览器能力路由规则（高优先级）\n\n"
        "- 当用户明确要求使用浏览器，或任务需要打开/跳转真实页面、点击、输入、登录、"
        "购物车操作、截图、检查页面显示内容、读取指定站点搜索结果时，立即调用同步 "
        "`task_tool`，将 `subagent_type` 设为 `\"browser_agent\"`，并在 "
        "`task_description` 中写明完整目标。不要使用 `subagent_spawn` 启动 browser_agent。\n"
        "- Browser Agent 是同步的专用浏览器能力，不是可选的并行委派。即使用户没有明确说"
        "“子代理”，即使主流程必须等待浏览器结果，也应遵循本节规则；本节覆盖通用子代理"
        "规则中“未明确要求委派时不要调用”和“关键路径不要等待子代理”的限制。\n"
        "- 对上述浏览器意图，不要先调用 paid_search、web_search、fetch 或类似搜索/抓取工具"
        "做预检。只有用户仅要求一般资料检索且不关心指定页面交互时，才优先使用搜索/抓取；"
        "或 Browser Agent 已返回明确 blocker/partial，且替代来源仍能满足用户目标时再降级。\n"
        "- 不要通过 Bash、代码执行、子进程、Shell 命令或直接启动 Chrome/Edge "
        "来执行浏览器自动化。\n"
        "- `task_tool` 或 `browser_agent` 不可用时，明确说明不可用，不要改用命令行启动浏览器。"
    ),
    "en": (
        "## Browser Capability Routing Rules (High Priority)\n\n"
        "- When the user explicitly requests a browser, or the task requires opening or "
        "navigating a real page, clicking, typing, logging in, changing a cart, taking a "
        "screenshot, inspecting rendered content, or reading search results on a specified "
        "site, immediately call the synchronous `task_tool`, set `subagent_type` to "
        '`"browser_agent"`, and put the complete objective in `task_description`. Do not use '
        "`subagent_spawn` for browser_agent.\n"
        "- Browser Agent is a synchronous browser capability, not optional parallel delegation. "
        "Apply this rule even when the user did not explicitly request a subagent and even when "
        "the main path must wait for the result. This section overrides generic subagent rules "
        "that prohibit unrequested delegation or waiting on critical-path subagents.\n"
        "- For browser intent, do not preflight with paid_search, web_search, fetch, or similar "
        "search/fetch tools. Prefer those tools only for general information research that does "
        "not depend on a specified site's UI, or after Browser Agent returns an explicit "
        "blocker/partial result and an alternate source can still satisfy the user.\n"
        "- Do not use Bash, code execution, subprocesses, shell commands, or direct Chrome/Edge launches "
        "for browser automation.\n"
        "- If `task_tool` or `browser_agent` is unavailable, state that clearly; "
        "do not fall back to launching a browser from the command line."
    ),
}


def build_browser_task_prompt(language: str = "cn") -> str:
    """Return localized browser guidance for the subagent tool section."""

    return _BROWSER_TASK_PROMPT.get(language, _BROWSER_TASK_PROMPT["cn"])


__all__ = ["build_browser_task_prompt"]
