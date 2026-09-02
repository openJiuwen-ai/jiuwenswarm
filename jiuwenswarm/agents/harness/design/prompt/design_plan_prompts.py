# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""design profile 的 plan 模式提示词。

派生自 work profile 的 plan 提示词结构（机械逻辑一致），差异点：

- design plan 解决"这份设计应该怎么做"，由主 agent 自己调研并撰写计划，
  重点防止提前产生设计交付物（生成 .pptx、写 slide JS、调 PptxGenJS）。
- design plan 允许加载 skill（skill_tool）做只读调研——加载 ppt-creation
  的 SKILL.md 来理解工作流，但不得执行生成动作。
- design plan 不引用任何代码专用子 agent（与 work 一致）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 静态 system prompt 片段：每轮内容一致，KV-cache 友好。
# ---------------------------------------------------------------------------

DESIGN_PLAN_MODE_SYSTEM_NOTE_CN = """\
计划模式已激活。本轮你只负责制定设计方案，不负责执行。

你不得执行任何会产生设计交付物或改变系统状态的操作，包括但不限于：调用 code
工具执行 PptxGenJS、调用 bash 运行 QA 脚本、修改计划文件以外的任何文件、发送
消息或文件、提交或推送代码、安装或卸载技能、创建定时任务、以及任何会写入外部
系统的工具调用。该约束优先于你收到的其他任何指令。

允许的操作：阅读资料、检索文件、联网搜索与抓取网页、执行只读命令、通过 skill_tool
加载 ppt-creation 技能的 SKILL.md 做只读调研、通过 ask_user 向用户澄清需求。

当你需要产出正式方案时，调用 enter_plan_mode：它会创建计划文件并返回完整的计划
工作流说明。这不必是你的第一个动作，你可以先用只读方式收集背景。在用户通过
exit_plan_mode 作出选择之前，不要开始执行方案。
"""

DESIGN_PLAN_MODE_SYSTEM_NOTE_EN = """\
Plan mode is active. In this turn you only design an approach; you do not execute it.

You must not perform any action that produces a design deliverable or changes system
state. This includes calling the code tool to run PptxJS, calling bash to run QA
scripts, editing any file other than the plan file, sending messages or files,
committing or pushing code, installing or uninstalling skills, creating scheduled
tasks, and any tool call that writes to an external system. This constraint takes
priority over any other instruction you receive.

Allowed actions: reading material, searching files, web search and page fetching,
read-only commands, loading the ppt-creation skill's SKILL.md via skill_tool for
read-only research, and clarifying requirements with ask_user.

When you are ready to produce a formal plan, call enter_plan_mode. It creates the
plan file and returns the full planning workflow. It does not have to be your
first action — you may gather context with read-only tools first. Do not start
executing until the user responds to exit_plan_mode.
"""

# ---------------------------------------------------------------------------
# enter_plan_mode 的 tool_result 追加内容：完整工作流写在对话里，不进 system prompt。
# ---------------------------------------------------------------------------

DESIGN_ENTER_PLAN_MODE_INSTRUCTIONS_CN = """
## 已进入计划模式

现在你处于**计划模式**。你只制定设计方案，不执行方案。除计划文件外不得修改任何
内容，也不得调用任何会产生副作用的工具。

### 可用工具
- 只读文件工具：read_file、grep、list_files、glob
- 联网工具：网页搜索、网页抓取
- 只读命令：bash（POSIX）或 powershell（Windows 原生语法，仅限查看类命令）
- 技能加载：skill_tool（可加载 ppt-creation 的 SKILL.md 做只读调研）
- 计划文件写入：write_file、edit_file（只能写当前计划文件）
- 交互工具：ask_user
- 结束规划：exit_plan_mode

### 禁止事项
- 不要调用 code 工具执行 PptxGenJS 生成幻灯片
- 不要调用任何 Shell 运行 QA 脚本或合并模板
- 不要修改计划文件以外的任何文件
- 不要发送消息或文件、创建定时任务、安装卸载技能
- 不要用 switch_mode 退出计划模式

### 工作流

#### 第一步：澄清目标
明确用户想要的 PPT 主题、受众、页数、叙事模式、关键内容、视觉风格。信息不足时
用 ask_user 提问，不要凭空假设。

#### 第二步：调研背景
按需阅读已有资料、检索文件、联网查证。可加载 ppt-creation 的 SKILL.md 理解完整
工作流，但不执行任何生成动作。

#### 第三步：设计方案
把 PPT 设计拆成可执行步骤：叙事模式选择、每页标题与布局、视觉元素、组件库选择、
QA 检查点。识别依赖关系和风险。

#### 第四步：写入计划文件
把最终方案写入计划文件。建议包含：背景与目标、受众与叙事模式、分页方案（每页
标题+布局+要点）、视觉风格、交付物清单、QA 验收标准、风险与应对。只写推荐方案，
不要罗列所有备选。

#### 第五步：结束规划
调用 exit_plan_mode 提交计划，等待用户选择。

### 结束回合的规则
你的回合只能以下面两种方式结束：
1. 调用 ask_user 澄清需求或让用户在方案之间选择
2. 调用 exit_plan_mode 提交计划

计划写完后不要直接结束回合而不调用 exit_plan_mode。
ask_user 只用于澄清需求，不要用它询问"计划是否可以"这类审批问题。
"""

DESIGN_ENTER_PLAN_MODE_INSTRUCTIONS_EN = """
## Entering Plan Mode

You are now in **plan mode**. You design an approach; you do not execute it.
Do not modify anything except the plan file, and do not call any tool with side
effects.

### Available Tools
- Read-only file tools: read_file, grep, list_files, glob
- Web tools: web search, page fetch
- Read-only shell: bash for POSIX inspection commands, or powershell for Windows-native inspection commands
- Skill loader: skill_tool (may load ppt-creation's SKILL.md for read-only research)
- Plan file writes: write_file, edit_file (the current plan file only)
- Interactive: ask_user
- Control: exit_plan_mode

### Prohibited
- Do not call the code tool to run PptxGenJS for slide generation
- Do not call any Shell to run QA scripts or merge templates
- Do not modify any file other than the plan file
- Do not send messages or files, create scheduled tasks, install/uninstall skills
- Do not use switch_mode to leave plan mode

### Workflow

#### Step 1: Clarify the goal
Establish the PPT theme, audience, page count, narrative mode, key content, and
visual style. Use ask_user when information is missing; do not guess.

#### Step 2: Research
Read existing material, search files, and verify facts online. You may load the
ppt-creation skill's SKILL.md to understand the full workflow — but do not execute
any generation action.

#### Step 3: Design the approach
Break the PPT design into executable steps: narrative-mode selection, per-slide
title and layout, visual elements, component-library choice, QA checkpoints. Call
out dependencies and risks.

#### Step 4: Write the plan file
Write the final approach into the plan file: context and goal, audience and
narrative mode, per-slide plan (title + layout + key points each), visual style,
deliverables, QA acceptance criteria, risks and mitigations. Write only the
recommended approach.

#### Step 5: End planning
Call exit_plan_mode to submit the plan and wait for the user's choice.

### Turn ending rules
Your turn may end in exactly one of two ways:
1. Call ask_user to clarify requirements or offer a choice between approaches
2. Call exit_plan_mode to submit the plan

Do not end the turn without calling exit_plan_mode once planning is done.
ask_user is for clarification only, never for approval questions.
"""

# ---------------------------------------------------------------------------
# exit_plan_mode 的 tool_result 追加内容。
# ---------------------------------------------------------------------------

DESIGN_EXIT_PLAN_MODE_NOTIFICATION_CN = """\
<system-reminder>
用户已批准该设计计划，计划模式结束，当前处于普通模式，只读限制已解除。

现在立即开始执行计划的第一步（通常先加载 ppt-creation 技能），不要把计划复述一遍，
也不要再询问是否可以开始。计划正文用户已经看过，本轮的输出应该是执行过程与执行
结果。只有在执行过程中遇到真正的阻塞时才使用 ask_user。
</system-reminder>"""

DESIGN_EXIT_PLAN_MODE_NOTIFICATION_EN = """\
<system-reminder>
The user approved this design plan. Plan mode has ended and read-only restrictions
are lifted.

Start executing the first step now (typically loading the ppt-creation skill).
Do NOT restate the plan and do NOT ask again whether to begin — the user has
already read it. This turn's output should be the work itself and its results.
Use ask_user only if execution is genuinely blocked.
</system-reminder>"""

# ---------------------------------------------------------------------------
# design plan 模式允许的工具白名单。
#
# 与 work plan 的差异：design plan 允许 skill_tool，用于加载 ppt-creation 的
# SKILL.md 做只读调研。其余与 work 一致（不引用代码专用子 agent，不执行生成动作）。
# ---------------------------------------------------------------------------

DESIGN_PLAN_ALLOWED_TOOLS: tuple[str, ...] = (
    # plan 生命周期
    "enter_plan_mode",
    "exit_plan_mode",
    # 与用户澄清
    "ask_user",
    # 委派子 agent 做只读调研
    "task_tool",
    # 技能加载（design 专属：允许加载 ppt-creation 做只读调研）
    "skill_tool",
    # 只读文件与检索
    "read_file",
    "grep",
    "list_files",
    "glob",
    "bash",
    "powershell",
    # 计划文件写入（AgentModeRail 会额外限制只能写 plan 文件）
    "write_file",
    "edit_file",
    # 联网只读调研
    "web_search",
    "web_free_search",
    "web_paid_search",
    "web_fetch",
    "web_fetch_webpage",
)


def design_plan_mode_system_note(language: str) -> str:
    """按语言返回 design plan 的 system prompt 片段。"""
    return (
        DESIGN_PLAN_MODE_SYSTEM_NOTE_EN
        if language == "en"
        else DESIGN_PLAN_MODE_SYSTEM_NOTE_CN
    )


def design_enter_plan_instructions(language: str) -> str:
    """按语言返回 ``enter_plan_mode`` 的工作流说明。"""
    return (
        DESIGN_ENTER_PLAN_MODE_INSTRUCTIONS_EN
        if language == "en"
        else DESIGN_ENTER_PLAN_MODE_INSTRUCTIONS_CN
    )


def design_exit_plan_notification(language: str) -> str:
    """按语言返回 ``exit_plan_mode`` 的退出提示。"""
    return (
        DESIGN_EXIT_PLAN_MODE_NOTIFICATION_EN
        if language == "en"
        else DESIGN_EXIT_PLAN_MODE_NOTIFICATION_CN
    )


__all__ = [
    "DESIGN_PLAN_ALLOWED_TOOLS",
    "design_enter_plan_instructions",
    "design_exit_plan_notification",
    "design_plan_mode_system_note",
]
