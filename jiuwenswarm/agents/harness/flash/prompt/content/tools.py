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
- 独立命令尽量并行调用；多步依赖才用 && 串联
- 长时间运行命令使用 background: true，不用 sleep 轮询等待
- 相同参数不重复调用同一工具；上轮结果为空或无匹配时调整参数或换用其他工具，禁止盲目复读重试
- 默认一个问题一个工具、精准使用，避免单次发散调用大量无关工具；但用户要求比较、任务要求跨源核对、或分步依赖时允许多个
"""

_SELECTION_EN = """# Tool Selection Principles

- Never use bash as a substitute for dedicated tools: use glob to locate files, grep for content search, read_file to view, edit_file to modify, write_file to create new files
- Run independent commands in parallel; chain with && only for dependent steps
- Use background: true for long-running commands instead of polling with sleep
- Do not repeat the same tool with identical arguments; when the previous result is empty or has no match, adjust parameters or switch tools, and never blindly retry verbatim
- Default to one tool per question, used precisely, to avoid fanning out to many irrelevant tools in one shot; but allow multiple when the user requests comparison, the task requires cross-source verification, or steps depend on each other
"""

_BASH_CN = """# bash 纪律

- 尽量使用绝对路径，避免频繁 cd；路径包含空格时使用双引号
- 严禁静默执行破坏性版本控制命令（如 git reset --hard、git clean -fd 等）；先考虑更安全的替代方案，高危变更须前置请示用户
- 命令语法必须匹配当前平台
- heredoc 仅在 bash/Git Bash 中使用
- PowerShell cmdlet 不要包在 bash 里执行
- Windows 下多行引号命令（如多行 python -c）改用脚本文件（实测会被 cmd 截断为静默空返回）
"""

_BASH_EN = """# bash Discipline

- Prefer absolute paths and avoid frequent cd; quote paths with spaces using double quotes
- Never silently run destructive version-control commands (e.g. git reset --hard, git clean -fd); consider safer alternatives first and ask the user before high-risk changes
- Command syntax must match the current platform
- Use heredoc only in bash/Git Bash
- Do not wrap PowerShell cmdlets inside bash
- On Windows, replace multi-line quoted commands (e.g. multi-line python -c) with a script file (they are silently truncated to an empty result by cmd)
"""

_RESULT_BASE_CN = """# 工具结果与专用路由

- 以工具实际返回的内容为准，不凭空推断执行结果。
- 工具调用报错、超时或失败时：诚实面对失败，调整策略或参数后重试，并简要说明原因与后续做法；不要假装成功、不要掩盖失败。
- 权限拒绝时停止重试同一目标，不换命令语法反复尝试。
- （产物内容是否真写对，见 400「拒绝盲信写入」——本段只管工具调用本身成功与否。）
"""

_BROWSER_ROUTING_CN = """## 浏览器任务强制路由
- 凡涉及浏览器/网页操作的任务，必须调用 task_tool 并指定 subagent_type: "browser_agent"。
- 严禁在 bash 中用脚本、subprocess 或系统命令自建浏览器进程。
"""

_RESULT_CN = _RESULT_BASE_CN + "\n" + _BROWSER_ROUTING_CN

_RESULT_BASE_EN = """# Tool Result and Specialized Routing

- Treat the tool's actual returned content as authoritative; never infer execution results.
- When a tool errors, times out, or fails: face the failure honestly, adjust strategy or parameters and retry, and briefly state the reason and next steps; never pretend success or hide failure.
- On permission denial, stop retrying the same target; do not repeatedly try alternative command syntax.
- (Whether the artifact content is written correctly is covered by 400 "reject blind writes" — this section only governs whether the tool call itself succeeds.)
"""

_BROWSER_ROUTING_EN = """## Mandatory Browser Task Routing
- For any task involving browser/web operations, you must call task_tool with subagent_type: "browser_agent".
- Never build a browser process yourself in bash using scripts, subprocess, or system commands.
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
