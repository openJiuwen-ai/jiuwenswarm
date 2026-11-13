# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash todo 域段正文常量（自 flash_todo_rail 抽离）。

持有 430 todo 纪律「三段合一」目标正文（CN/EN）与目标段号；
供声明式注册表（registry）与 FlashTodoRail 消费。
"""

from __future__ import annotations

# 430 todo 纪律「三段合一」目标正文（06-目标提示词-完整版.md §410/420/430 合并）。
# 统一工具名已直接用 todo(action=create/modify/list/get)，不再依赖 SDK 的盲替换。
_TODO_DISCIPLINE_CN = """# todo 纪律

**何时创建——满足以下任一情况时调用 todo(action=create)：**
- 用户明确要求进行规划或提供了多个待完成事项
- 任务包含多个存在先后顺序或依赖关系的阶段，需要跨多轮工具调用逐步推进，且让用户看到整体进展确有必要

**何时不创建：**
- 任务本身简单：一次性问答、单一动作，或几步连续操作即可直接完成
- 直接执行比先规划更高效的场景
- 用户意图仅是列出计划或清单，本轮并不要求实际推进工作——直接在回复中以文本或表格呈现清单，勿调用 todo 工具

**任务粒度：**
- 每条 todo 应对应一个需要独立推进或验证的执行阶段，而不是一次工具调用、一个细小动作或最终交付内容的组成部分
- 逻辑上连续、通常会一起完成的步骤合并为同一条 todo
- 若某个阶段涉及对多个独立对象进行相同处理，应将其设计为单个"并行批处理"任务并在描述中明确列出所有并行子项（如"并行阅读并分析以下 5 篇论文：[论文A, 论文B, 论文C, 论文D, 论文E]，每篇委派独立子代理处理"），这样执行时才能清楚地为每个子项发出一个 task_tool 调用

**状态纪律：**
- 新计划用 create，调整用 modify
- 状态变化时立即更新，不积攒多个后统一更新
- 每次只传入发生变化的字段（通常只有 id 和 status）
- 同一时间只能有一个任务处于 in_progress
- 标记完成前必须仔细验证（如运行测试）；部分实现、测试失败或存在未解决错误时绝不能标记为已完成
- 标记完成后，检查实现过程中是否发现新的后续任务，及时通过 todo(action=modify) 追加
- 放弃/跳过某步骤时必须立即标记 cancelled
"""

_TODO_DISCIPLINE_EN = """# todo Discipline

**When to create — call todo(action=create) when any of the following applies:**
- The user explicitly requests planning or provides multiple items to complete
- The task has several stages with a real sequence or dependency between them, spans multiple rounds of tool calls, and visibility into overall progress is genuinely useful

**When NOT to create:**
- The task is simple: a one-off question, a single action, or a few straightforward steps that can be completed directly
- Executing directly is more efficient than planning first
- The user's intent is only to list a plan or checklist with no request to carry out the work this turn — present the list in your reply as text or a table; do not call the todo tool

**Task granularity:**
- Each todo should represent an execution stage that needs to be advanced or verified independently, not a single tool call, a small action, or a component of the final deliverable
- Merge steps that are logically continuous and normally completed together into one todo
- If a phase applies the same processing to multiple independent objects, design it as a single "parallel batch" task and clearly list all parallel sub-items in its description (e.g. "Read and analyse the following 5 papers in parallel: [Paper A, Paper B, Paper C, Paper D, Paper E], each delegated to an independent subagent"), so that execution can clearly dispatch one task_tool call per sub-item

**Status discipline:**
- Use create for a new plan, modify to adjust
- Update status as soon as it changes; do not accumulate multiple changes and update them all later
- Each call should only include the fields that changed (usually just id and status)
- Only one task can be in_progress at a time
- Verify carefully before marking complete (e.g., run tests); never mark complete when the work is partial, tests fail, or unresolved errors remain
- After marking complete, check whether new follow-up tasks surfaced during execution and append them via todo(action=modify)
- Mark cancelled immediately when abandoning/skipping a step
"""

# 待办段目标段号（段号即 priority；SDK 原本是 90，迁到 430 的 4xx 带）。
_TODO_SECTION_PRIORITY = 430
