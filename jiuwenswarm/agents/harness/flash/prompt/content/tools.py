# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 工具域段正文 + 剥离纯函数（自 flash_tool_guide_rail 抽离）。

持有 300/330/340 的 06 目标正文（CN/EN）、源段名/目标段名/段号、子标题锚点与
``_strip_tools_guide`` 纯函数，供声明式注册表（registry）与 FlashToolGuideRail 消费。
"""

from __future__ import annotations

import re

# ── 源段名 ──────────────────────────────────────────────
_SECTION_TOOLS = "tools"           # SDK ContextAssembleRail 构建，priority 30
_SECTION_EXEC = "execution_principles"  # FlashPersonaRail 拆出的 400

# ── 目标段名 / 段号 ──────────────────────────────────────
_NAME_SELECTION = "tool_selection"
_PRIO_SELECTION = 300
_NAME_BASH = "bash_discipline"
_PRIO_BASH = 330
_NAME_RESULT = "tool_result"
_PRIO_RESULT = 340

# ── tools 段内的子标题（用于定位剥离边界）──────────────────
_TOOLS_DEDUP_CN = "## 工具调用去重规则"
_TOOLS_DEDUP_EN = "## Tool Call Deduplication Rules"
_TOOLS_BASH_CN = "## bash 使用原则"
_TOOLS_BASH_EN = "## bash Guidelines"
_TOOLS_TASKTOOL_CN = "## task_tool 使用原则"
_TOOLS_TASKTOOL_EN = "## task_tool Guidelines"


# ═══ 目标正文（06-目标提示词-完整版.md §3xx）═══════════════════════
_SELECTION_CN = """# 工具选择总则

- 严禁用 bash 替代专用工具：文件定位用 glob、内容检索用 grep、查看用 read_file、改动用 edit_file、创建新文件用 write_file
- 独立命令尽量并行调用；多步依赖才用 && 串联（仅在不关心前序失败时才用 ;）
- 长时间运行命令使用 background: true，不用 sleep 轮询等待
- 相同参数不重复调用同一工具（除非文件或环境状态已变化）；上轮结果为空或无匹配时调整参数或换用其他工具，禁止盲目复读重试
- 默认一个问题一个工具、精准使用，避免单次发散调用大量无关工具；但用户要求比较、任务要求跨源核对、或分步依赖时允许多个
"""

_SELECTION_EN = """# Tool Selection Principles

- Never use bash as a substitute for dedicated tools: use glob to locate files, grep for content search, read_file to view, edit_file to modify, write_file to create new files
- Run independent commands in parallel; chain with && only for dependent steps (use ; only when you don't care about prior-step failure)
- Use background: true for long-running commands instead of polling with sleep
- Do not repeat the same tool with identical arguments (unless the file or environment state has changed); when the previous result is empty or has no match, adjust parameters or switch tools, and never blindly retry verbatim
- Default to one tool per question, used precisely, to avoid fanning out to many irrelevant tools in one shot; but allow multiple when the user requests comparison, the task requires cross-source verification, or steps depend on each other
"""

_BASH_CN = """# bash 纪律

- 跨目录操作时尽量使用绝对路径，避免频繁 cd；路径包含空格时必须使用双引号
- 严禁静默执行破坏性版本控制命令（如 git reset --hard、git clean -fd 等）；先考虑更安全的替代方案，高危变更须前置请示用户
- 命令语法必须匹配当前平台 Shell：
  - heredoc（<<EOF）仅在 bash/Git Bash 中使用，cmd/PowerShell 不支持
  - PowerShell cmdlet 不要包在 bash 里执行
  - Windows 下严禁使用多行引号命令（如多行 python -c），必须改用脚本文件执行（防止被 cmd 静默截断为空返回）
"""

_BASH_EN = """# bash Discipline

- Prefer absolute paths when operating across directories, avoid frequent cd; always quote paths containing spaces with double quotes
- Never silently run destructive version-control commands (e.g. git reset --hard, git clean -fd); consider safer alternatives first and ask the user before high-risk changes
- Command syntax must match the current platform shell:
  - heredoc (<<EOF) only in bash/Git Bash; cmd/PowerShell do not support it
  - Do not wrap PowerShell cmdlets inside bash
  - On Windows, never use multi-line quoted commands (e.g. multi-line python -c); use a script file instead (prevents silent truncation to an empty result by cmd)
"""

_RESULT_BASE_CN = """# 工具结果与专用路由

- 严格以工具实际返回的内容为准，严禁凭空推断或脑补执行结果。
- 工具调用报错、超时或失败时：诚实面对失败，调整策略或参数后重试，并简要说明原因与后续做法；不要假装成功、不要掩盖失败。
- 遇到权限拒绝（Permission denied）或路径越权时，立即停止对同一目标的重试，严禁换命令语法反复尝试绕过。
"""

_BROWSER_ROUTING_CN = """## 浏览器任务强制路由
- 凡涉及浏览器/网页交互的任务，必须调用 task_tool 并指定 subagent_type: "browser_agent"（若当前环境未提供该工具，直接向用户说明限制）。
- 严禁通过 bash 等工具用脚本、subprocess 或系统命令自建浏览器进程（如 playwright/selenium/puppeteer）。
"""

_RESULT_CN = _RESULT_BASE_CN + "\n" + _BROWSER_ROUTING_CN

_RESULT_BASE_EN = """# Tool Result and Specialized Routing

- Strictly treat the tool's actual returned content as authoritative; never infer or fabricate execution results.
- When a tool errors, times out, or fails: face the failure honestly, adjust strategy or parameters and retry, and briefly state the reason and next steps; never pretend success or hide failure.
- On permission denial (Permission denied) or path out-of-bounds, stop retrying the same target immediately; never bypass by repeatedly trying alternate command syntax.
"""

_BROWSER_ROUTING_EN = """## Mandatory Browser Task Routing
- For any task involving browser/web interaction, you must call task_tool with subagent_type: "browser_agent" (if the tool is not available in the current environment, tell the user the limitation directly).
- Never build a browser process yourself via bash or other tools using scripts, subprocess, or system commands (e.g. playwright/selenium/puppeteer).
"""

_RESULT_EN = _RESULT_BASE_EN + "\n" + _BROWSER_ROUTING_EN


_SUBSECTION_RE = re.compile(r"^##\s[^\n]*", re.MULTILINE)


def _strip_tools_guide(
    content: str,
    dedup_header: str,
    bash_header: str,
    tasktool_header: str,
) -> str | None:
    """从 tools 段内容剔除「工具调用去重规则 + bash 使用原则」两小节。

    保留 ``# 可用工具`` 目录与 ``task_tool 使用原则``（后者归 440，本 rail 不碰）。
    以 dedup/bash/task_tool 三锚点确认剔除边界：dedup 缺失、或剔除区内出现未识别的
    ``## `` 子标题（上游改文案/新增小节）时返回 None，不盲删。
    """
    dedup_idx = content.find(dedup_header)
    if dedup_idx < 0:
        return None  # 守卫：dedup 子标题必在（bash 可缺席）
    catalog = content[:dedup_idx]
    tail = content[dedup_idx:]

    tasktool_idx = tail.find(tasktool_header)
    strip_zone = tail[:tasktool_idx] if tasktool_idx >= 0 else tail
    if not _only_known_subsections(strip_zone, {dedup_header, bash_header}):
        return None  # 守卫：剔除区出现未知子标题，上游漂移，不盲删
    if tasktool_idx >= 0:
        return catalog + tail[tasktool_idx:]
    return catalog


def _only_known_subsections(zone: str, known_headers: set[str]) -> bool:
    """zone 内每个 ``## `` 子标题都必须落在 known_headers；否则视为上游漂移。"""
    for match in _SUBSECTION_RE.finditer(zone):
        header = match.group(0).strip()
        if header and header not in known_headers:
            return False
    return True
