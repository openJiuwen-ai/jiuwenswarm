# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Desktop (cua_agent) guidance appended to agent-core's subagent tool section."""

from __future__ import annotations


_CUA_TASK_PROMPT = {
    "cn": (
        "## 桌面能力路由规则（高优先级）\n\n"
        "- 当用户要求操作本机应用或桌面——启动应用、在原生窗口（Office、IDE、聊天客户端、"
        "系统对话框、设置面板等）内点击或输入、按快捷键、滚动、读取某个窗口当前显示的内容、"
        "查看有哪些应用/窗口处于打开状态时，立即调用同步 `task_tool`，将 `subagent_type` "
        '设为 `"cua_agent"`，并在 `task_description` 中写明完整目标。不要使用 '
        "`subagent_spawn` 启动 cua_agent。\n"
        "- Desktop Agent 是同步的专用桌面能力，不是可选的并行委派。即使用户没有明确说"
        "“子代理”，即使主流程必须等待桌面结果，也应遵循本节规则；本节覆盖通用子代理"
        "规则中“未明确要求委派时不要调用”和“关键路径不要等待子代理”的限制。\n"
        "- `task_description` 必须保留用户的完整动作目标，不得把“在记事本里输入并保存”"
        "缩减为步骤说明。发送消息、删除文件、提交表单、支付等不可逆操作，仍须在桌面任务"
        "执行前取得用户最终确认。\n"
        "- 不要通过 Bash、PowerShell、代码执行、子进程、AppleScript、AutoHotkey、pyautogui "
        "等方式驱动应用窗口；桌面自动化只走 cua_agent。无需图形界面的普通文件/命令行工作"
        "仍由你自己的工具完成。\n"
        "- `task_tool` 或 `cua_agent` 不可用时，明确说明不可用，不要改用命令行脚本操作图形界面。"
    ),
    "en": (
        "## Desktop Capability Routing Rules (High Priority)\n\n"
        "- When the user asks to operate a local application or the host desktop, such as "
        "launching an app, clicking or typing inside a native window (Office, IDEs, chat "
        "clients, system dialogs, settings panels), pressing hotkeys, scrolling, reading what a "
        "window currently shows, or checking which apps and windows are open, immediately call "
        'the synchronous `task_tool`, set `subagent_type` to `"cua_agent"`, and put the '
        "complete objective in `task_description`. Do not use `subagent_spawn` for cua_agent.\n"
        "- Desktop Agent is a synchronous desktop capability, not optional parallel delegation. "
        "Apply this rule even when the user did not explicitly request a subagent and even when "
        "the main path must wait for the result. This section overrides generic subagent rules "
        "that prohibit unrequested delegation or waiting on critical-path subagents.\n"
        "- Preserve the user's complete action objective in `task_description`; do not narrow "
        '"type this into Notepad and save it" into a description of the steps. Irreversible '
        "actions (sending messages, deleting files, submitting forms, payments) still require the "
        "user's final confirmation before the desktop task performs them.\n"
        "- Do not use Bash, PowerShell, code execution, subprocesses, AppleScript, AutoHotkey, "
        "pyautogui, or similar to drive application windows; desktop automation goes through "
        "cua_agent only. Plain file and shell work that needs no GUI stays with your own tools.\n"
        "- If `task_tool` or `cua_agent` is unavailable, state that clearly; do not fall back to "
        "scripting the GUI from the command line."
    ),
}

_BROWSER_VS_CUA_PROMPT = {
    "cn": (
        "### 浏览器与桌面能力的选择\n\n"
        "- 目标位于网页中（URL、网站、Web 应用、登录某个站点）→ `browser_agent`。\n"
        "- 目标位于本机原生窗口中（已安装的应用、系统对话框、桌面本身）→ `cua_agent`。\n"
        "- 任务同时跨越两者（例如把网页上的数值填进桌面应用）→ 先用 `browser_agent` 完成"
        "网页部分，再用 `cua_agent` 完成桌面部分；不要让其中一个代理去做另一个的工作。"
    ),
    "en": (
        "### Choosing between browser and desktop\n\n"
        "- The target lives in a web page (URLs, websites, web apps, logging in to a site): "
        "use `browser_agent`.\n"
        "- The target lives in a native window on this machine (installed apps, system dialogs, "
        "the desktop itself): use `cua_agent`.\n"
        "- The task spans both (for example, copying a value from a website into a desktop "
        "app): run the browser part with `browser_agent` first, then the desktop part with "
        "`cua_agent`. Do not ask either agent to do the other's job."
    ),
}


_CUA_UNAVAILABLE_PROMPT = {
    "cn": (
        "## 桌面能力不可用\n\n"
        "- 桌面子代理 cua_agent 已在配置中启用，但当前模型无法理解图片输入，而 cua_agent "
        "只能通过截图观察屏幕，因此本会话未挂载它。\n"
        "- 当用户要求操作本机应用或桌面时，直接说明这一点，并建议用户切换到支持图片输入的模型"
        "后重试。不要改用 Bash、PowerShell、代码执行、pyautogui 等方式驱动图形界面。"
    ),
    "en": (
        "## Desktop Capability Unavailable\n\n"
        "- The desktop subagent cua_agent is enabled in the configuration, but the current "
        "model cannot read image input and cua_agent sees the screen only through "
        "screenshots, so it is not mounted in this session.\n"
        "- When the user asks to operate a local application or the host desktop, say so "
        "directly and suggest switching to a model that supports image input, then retrying. "
        "Do not drive the GUI through Bash, PowerShell, code execution, pyautogui, or similar."
    ),
}


_CUA_BACKEND_UNAVAILABLE_PROMPT = {
    "cn": (
        "## 桌面能力不可用\n\n"
        "当前环境没有可用的电脑操控后端。用户要求操作本机应用或桌面时，请说明这一点，"
        "不要通过命令行或脚本操作图形界面。"
    ),
    "en": (
        "## Desktop Capability Unavailable\n\n"
        "No computer control backend is available on this host. If the user asks to operate a "
        "local app or the desktop, explain this and do not drive the GUI through shell commands or scripts."
    ),
}


def build_cua_unavailable_prompt(language: str = "cn", *, backend_available: bool = True) -> str:
    """Return guidance for the host or model capability that prevented desktop access."""

    prompts = _CUA_UNAVAILABLE_PROMPT if backend_available else _CUA_BACKEND_UNAVAILABLE_PROMPT
    return prompts.get(language, prompts["cn"])


def build_cua_repl_prompt(language: str, *, legacy_available: bool, api: str | None) -> str:
    """Return localized guidance for the desktop subagent's REPL."""
    if language == "en":
        intro = (
            "Use single-step tools for simple actions and cua_repl for multi-step workflows. "
            "Keep their references and screenshot coordinates separate."
            if legacy_available
            else "Operate the host desktop through cua_repl. Only act on apps needed for the user's task. "
            "Leave web page automation to the browser agent. Verify the outcome by observing it."
        )
        instructions = (
            "cua_repl executes Python. computer, print and print_image are preloaded. "
            "Call SDK methods synchronously. Variables persist within this delegation. "
            "The host closes the session afterwards. Reads present text automatically. "
            "Use print_image for images and read again to verify actions. "
            "After a timeout or cancellation, actions may already have executed. Never replay them "
            "automatically. This code runs with the host user's permissions."
        )
        api_heading = "Public Computer Use SDK interface"
    else:
        intro = (
            "简单动作使用单步工具，多步流程使用 cua_repl。两类工具的引用和截图坐标分别使用。"
            if legacy_available
            else "使用 cua_repl 操作本机桌面，仅操作用户任务所需的应用。"
            "网页自动化交给浏览器子代理。操作后观察结果以确认是否完成。"
        )
        instructions = (
            "cua_repl 执行 Python，已预载 computer、print 和 print_image。"
            "直接调用同步 SDK 方法。变量在本次委派期间保留，委派结束后由宿主关闭会话。"
            "读取会自动显示文本，图片使用 print_image。操作后重新读取以验证结果。"
            "超时或取消时，动作可能已执行，禁止自动重放。代码以宿主用户的权限运行。"
        )
        api_heading = "Computer Use SDK 公共接口"
    prompt = f"{intro}\n{instructions}"
    if api:
        prompt += f"\n{api_heading}\n```python\n{api}\n```"
    return prompt


def build_cua_task_prompt(
    language: str = "cn", *, browser_available: bool = False
) -> str:
    """Return localized desktop guidance for the subagent tool section.

    When ``browser_available`` is set, a short rule for choosing between
    ``browser_agent`` and ``cua_agent`` is appended.
    """

    text = _CUA_TASK_PROMPT.get(language, _CUA_TASK_PROMPT["cn"])
    if browser_available:
        text += "\n\n" + _BROWSER_VS_CUA_PROMPT.get(
            language, _BROWSER_VS_CUA_PROMPT["cn"]
        )
    return text


__all__ = ["build_cua_repl_prompt", "build_cua_task_prompt", "build_cua_unavailable_prompt"]
