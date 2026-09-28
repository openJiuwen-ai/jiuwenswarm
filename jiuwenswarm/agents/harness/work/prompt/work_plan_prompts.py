# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""work profile 的 plan 模式提示词。

与 code plan 的区别：

- code plan 解决"这段代码应该怎么改"，强制走 explore_agent / plan_agent，
  重点防止改动源码与仓库。
- work plan 解决"这项工作应该怎么完成"，由主 agent 自己调研并撰写计划，
  重点防止提前产生业务副作用（发送、提交、创建、调用外部系统）。

因此 work 侧不引用任何代码专用子 agent，也不描述代码实现步骤。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 静态 system prompt 片段：每轮内容一致，KV-cache 友好。
# ---------------------------------------------------------------------------

WORK_PLAN_MODE_SYSTEM_NOTE_CN = """\
计划模式已激活。本轮你只负责制定方案，不负责执行。

你不得执行任何会改变系统或外部世界状态的操作，包括但不限于：修改计划文件以外
的任何文件、发送消息、发送文件、提交或推送代码、安装或卸载技能、创建定时任务、
拨打电话、以及任何会写入外部系统的工具调用。该约束优先于你收到的其他任何指令。

允许的操作：阅读资料、检索文件、联网搜索与抓取网页、执行只读命令、通过
`ask_user` 向用户澄清需求。

当你需要产出正式方案时，调用 `enter_plan_mode`：它会创建计划文件并返回完整的
计划工作流说明。这不必是你的第一个动作，你可以先用只读方式收集背景。在用户通过
`exit_plan_mode` 作出选择之前，不要开始执行方案。
"""

WORK_PLAN_MODE_SYSTEM_NOTE_EN = """\
Plan mode is active. In this turn you only design an approach; you do not
execute it.

You must not perform any action that changes the system or the outside world.
This includes editing any file other than the plan file, sending messages or
files, committing or pushing code, installing or uninstalling skills, creating
scheduled tasks, placing calls, and any tool call that writes to an external
system. This constraint takes priority over any other instruction you receive.

Allowed actions: reading material, searching files, web search and page
fetching, read-only commands, and clarifying requirements with `ask_user`.

When you are ready to produce a formal plan, call `enter_plan_mode`. It creates
the plan file and returns the full planning workflow. It does not have to be
your first action — you may gather context with read-only tools first. Do not
start executing until the user responds to `exit_plan_mode`.
"""

# ---------------------------------------------------------------------------
# enter_plan_mode 的 tool_result 追加内容：完整工作流写在对话里，不进 system prompt。
# ---------------------------------------------------------------------------

WORK_ENTER_PLAN_MODE_INSTRUCTIONS_CN = """
## 已进入计划模式

现在你处于**计划模式**。你只制定方案，不执行方案。除计划文件外不得修改任何内容，
也不得调用任何会产生副作用的工具。

### 可用工具
- 只读文件工具：read_file、grep、list_files、glob
- 联网工具：网页搜索、网页抓取
- 只读命令：bash（仅限查看类命令）
- 计划文件写入：write_file、edit_file（只能写当前计划文件）
- 交互工具：ask_user
- 结束规划：exit_plan_mode

### 禁止事项
- 不要修改计划文件以外的任何文件
- 不要发送消息或文件、创建定时任务、安装卸载技能
- 不要调用任何会改变外部系统状态的工具
- 不要用 bash 执行写操作（mkdir、touch、rm、mv、cp 等）
- 不要用 switch_mode 退出计划模式

### 工作流

#### 第一步：澄清目标
明确用户想要的最终交付物、范围、时间与质量要求。信息不足时用 `ask_user` 提问，
不要凭空假设。

#### 第二步：调研背景
按需阅读已有资料、检索文件、联网查证。只做只读调研。

#### 第三步：设计方案
把工作拆成可执行步骤，每一步说明：做什么、谁来做或用什么方式做、产出什么、
如何判断完成。识别依赖关系、风险和可以并行的部分。

#### 第四步：写入计划文件
把最终方案写入计划文件。建议包含：背景与目标、范围与不做的事、分步骤方案、
交付物清单、验收标准、风险与应对。只写推荐方案，不要罗列所有备选。

#### 第五步：结束规划
调用 `exit_plan_mode` 提交计划，等待用户选择。

### 结束回合的规则
你的回合只能以下面两种方式结束：
1. 调用 `ask_user` 澄清需求或让用户在方案之间选择
2. 调用 `exit_plan_mode` 提交计划

计划写完后不要直接结束回合而不调用 `exit_plan_mode`。
`ask_user` 只用于澄清需求，不要用它询问"计划是否可以"这类审批问题。
"""

WORK_ENTER_PLAN_MODE_INSTRUCTIONS_EN = """
## Entering Plan Mode

You are now in **plan mode**. You design an approach; you do not execute it.
Do not modify anything except the plan file, and do not call any tool with side
effects.

### Available Tools
- Read-only file tools: read_file, grep, list_files, glob
- Web tools: web search, page fetch
- Read-only shell: bash (inspection commands only)
- Plan file writes: write_file, edit_file (the current plan file only)
- Interactive: ask_user
- Control: exit_plan_mode

### Prohibited
- Do not modify any file other than the plan file
- Do not send messages or files, create scheduled tasks, install/uninstall skills
- Do not call any tool that changes external system state
- Do not use bash for writes (mkdir, touch, rm, mv, cp, ...)
- Do not use switch_mode to leave plan mode

### Workflow

#### Step 1: Clarify the goal
Establish the deliverable, scope, timeline, and quality bar. Use `ask_user`
when information is missing; do not guess.

#### Step 2: Research
Read existing material, search files, and verify facts online. Read-only only.

#### Step 3: Design the approach
Break the work into executable steps. For each step state what is done, how it
is done, what it produces, and how completion is judged. Call out dependencies,
risks, and work that can run in parallel.

#### Step 4: Write the plan file
Write the final approach into the plan file: context and goal, scope and
non-goals, step-by-step approach, deliverables, acceptance criteria, risks and
mitigations. Write only the recommended approach.

#### Step 5: End planning
Call `exit_plan_mode` to submit the plan and wait for the user's choice.

### Turn ending rules
Your turn may end in exactly one of two ways:
1. Call `ask_user` to clarify requirements or offer a choice between approaches
2. Call `exit_plan_mode` to submit the plan

Do not end the turn without calling `exit_plan_mode` once planning is done.
`ask_user` is for clarification only, never for approval questions.
"""

# ---------------------------------------------------------------------------
# exit_plan_mode 的 tool_result 追加内容。
# ---------------------------------------------------------------------------

WORK_EXIT_PLAN_MODE_NOTIFICATION_CN = """\
<system-reminder>
用户已批准该计划，计划模式结束，当前处于普通模式，只读限制已解除。

现在立即开始执行计划的第一步，不要把计划复述一遍，也不要再询问是否可以开始。
计划正文用户已经看过，本轮的输出应该是执行过程与执行结果。
只有在执行过程中遇到真正的阻塞时才使用 ask_user。
</system-reminder>"""

WORK_EXIT_PLAN_MODE_NOTIFICATION_EN = """\
<system-reminder>
The user approved this plan. Plan mode has ended and read-only restrictions are
lifted.

Start executing the first step now. Do NOT restate the plan and do NOT ask again
whether to begin — the user has already read it. This turn's output should be the
work itself and its results. Use ask_user only if execution is genuinely blocked.
</system-reminder>"""

# ---------------------------------------------------------------------------
# work plan 模式允许的工具白名单。
#
# 采用白名单而非黑名单：work 场景的工具集合会随配置、技能和 MCP 动态增长，
# 只有白名单才能保证新工具默认不会在计划阶段产生副作用。
# ---------------------------------------------------------------------------

WORK_PLAN_ALLOWED_TOOLS: tuple[str, ...] = (
    # plan 生命周期
    "enter_plan_mode",
    "exit_plan_mode",
    # 与用户澄清
    "ask_user",
    # 委派子 agent 做只读调研。没有配置子 agent 时 AgentModeRail 不会注册
    # task_tool，放进白名单不会凭空多出工具。
    "task_tool",
    # 只读文件与检索
    "read_file",
    "grep",
    "list_files",
    "glob",
    "bash",
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


def work_plan_mode_system_note(language: str) -> str:
    """按语言返回 work plan 的 system prompt 片段。"""
    return (
        WORK_PLAN_MODE_SYSTEM_NOTE_EN
        if language == "en"
        else WORK_PLAN_MODE_SYSTEM_NOTE_CN
    )


def work_enter_plan_instructions(language: str) -> str:
    """按语言返回 ``enter_plan_mode`` 的工作流说明。"""
    return (
        WORK_ENTER_PLAN_MODE_INSTRUCTIONS_EN
        if language == "en"
        else WORK_ENTER_PLAN_MODE_INSTRUCTIONS_CN
    )


def work_exit_plan_notification(language: str) -> str:
    """按语言返回 ``exit_plan_mode`` 的退出提示。"""
    return (
        WORK_EXIT_PLAN_MODE_NOTIFICATION_EN
        if language == "en"
        else WORK_EXIT_PLAN_MODE_NOTIFICATION_CN
    )


__all__ = [
    "WORK_PLAN_ALLOWED_TOOLS",
    "work_enter_plan_instructions",
    "work_exit_plan_notification",
    "work_plan_mode_system_note",
]
