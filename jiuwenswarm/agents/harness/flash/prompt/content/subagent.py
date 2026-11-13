# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 子代理域段正文常量（自 flash_subagent_rail 抽离）。

持有 440 子代理委派段正文（FLASH_TASK_SECTION，CN/EN）与工具卡描述 /
available_agents 文案；``FlashSubagentRail`` 从本模块 re-import，零行为变化。
"""

from __future__ import annotations

FLASH_TASK_SECTION_CN = """# 子代理委派（task_tool）

子代理在独立上下文中运行：其全部中间工具调用结果不进入你的主上下文，仅最终摘要返回。\
这是长任务保持主上下文清晰、避免能力随上下文膨胀而下降的关键手段。

何时委派（按上下文压力自主判断，非强制）：
- 任务的中间结果会显著膨胀主上下文：逐页深度研究（产出多篇研究文件）、\
大规模抓取/解析/转换、多轮迭代的修复循环
- 主上下文已经较长时，新的大块独立工作优先委派
- 多个互不依赖的子任务需要并行执行时：在同一条消息中一次性发出全部 task_tool 调用；\
每条消息只发一个调用等于串行等待，会成倍拉长总耗时

何时不委派（直接执行更快）：
- 一次性读取少量规范/配置/技能文档（SKILL.md 与若干 references 属于此列）
- 单步工具调用即可完成的简单操作
- 需要完整对话历史作为背景、或需要实时向用户流式输出的任务

使用要点：
- task_description 必须完整自包含——子代理没有本次对话的任何记忆
- 子代理结果对用户不可见；需要向用户展示时，另行发送文字总结
"""

FLASH_TASK_SECTION_EN = """# Subagent Delegation (task_tool)

Subagents run in an isolated context: none of their intermediate tool results \
enter your main context; only the final summary returns. This is the key lever \
for keeping the main context clean on long tasks.

When to delegate (judge autonomously by context pressure, not by mandate):
- The subtask's intermediate results would significantly bloat your context: \
per-page deep research, large-scale fetch/parse/transform, multi-round fix loops
- When your context is already long, prefer delegating new large independent work
- When multiple independent subtasks need to run in parallel: issue ALL task_tool \
calls together in a single message; one call per message serializes execution \
and multiplies wall-clock time

When NOT to delegate (doing it yourself is faster):
- Reading a handful of spec/config/skill documents (SKILL.md plus a few references)
- Simple operations completable in a single tool call
- Tasks needing full conversation history, or live streaming to the user

Notes:
- task_description must be fully self-contained — the subagent has no memory \
of this conversation
- Subagent results are not visible to the user; send a text summary when needed
"""

FLASH_TASK_SECTION: dict[str, str] = {
    "cn": FLASH_TASK_SECTION_CN,
    "en": FLASH_TASK_SECTION_EN,
}

FLASH_TASK_TOOL_DESCRIPTION_CN = """\
启动子代理在隔离上下文中执行复杂任务，仅返回最终摘要（中间工具结果不进入主上下文）。

可用代理类型：
{available_agents}

调用参数：
- subagent_type：必填，必须与上方列表中的名称完全一致
- task_description：完整自包含的任务描述——子代理没有本次对话的任何记忆，\
需写明目标、背景、期望产出与验收标准
- model_tier / model_name：可选，为该子代理指定 lite/pro 档位或精确模型；\
省略时使用当前默认模型

并行规则：多个互不依赖的子任务，必须在同一条消息中一次性发出全部 task_tool 调用；\
每条消息只发一个调用会导致串行等待。
"""

FLASH_TASK_TOOL_DESCRIPTION_EN = """\
Launch a subagent to execute a complex task in an isolated context; only the \
final summary returns (intermediate tool results stay out of your context).

Available subagent types:
{available_agents}

Parameters:
- subagent_type: required; must exactly match a name from the list above
- task_description: a fully self-contained task brief — the subagent has no \
memory of this conversation; state the goal, background, expected output, \
and acceptance criteria
- model_tier / model_name: optional; assign a lite/pro tier or an exact model \
to this subagent; omit to use the current default model

Parallel rule: for multiple independent subtasks, issue ALL task_tool calls \
together in a single message; issuing one call per message serializes execution.
"""

FLASH_TASK_TOOL_DESCRIPTION: dict[str, str] = {
    "cn": FLASH_TASK_TOOL_DESCRIPTION_CN,
    "en": FLASH_TASK_TOOL_DESCRIPTION_EN,
}

# flash 版 available_agents：general-purpose 的工具面语义化描述。
#
# stock `_build_available_agents_description` 只列 spec.tools（_get_tool_cards
# 注册的卡片：web_flash、deepresearch 系列等），漏掉 rail 继承在子代理构建时
# 注册的文件/bash 工具——描述会写成「继承文件读写、bash」却在 Tools
# 括号里只列 deepresearch/web_flash，自相矛盾，模型据此误判「子代理干不了
# 文件活」而放弃委派、退回全单干，废掉上下文隔离。flash 工具面为类常量固定，
# 语义化描述不漂移；非 general-purpose 的 spec 回退 stock 构建。
#
# 工具面与实际注册严格一致（CR-2）：gp 子代理通过 spec 的 rails 字段获得
# SlimSysOperationRail（flash 文件变体），通过 spec 的 tools 字段获得
# web_flash / deepresearch / skill 等工具卡。todo 的 flash rail（FlashTodoRail）
# 需要引擎初始化（todo 引擎），不适合在 spec 注入时简单传入，故不在此宣称。
FLASH_AVAILABLE_AGENTS_CN = (
    "- general-purpose: 通用型子代理，在隔离上下文中执行任务，仅最终摘要返回主上下文。"
    "可用工具（Tools: read_file、write_file、edit_file、glob、grep、"
    "bash、web_flash、deepresearch 系列等）——"
    "可读写文件、执行 CLI 命令、联网搜索与深度调研，适合逐页研究、页面生成/修复等重活。"
)

FLASH_AVAILABLE_AGENTS_EN = (
    "- general-purpose: general-purpose subagent executing tasks in an isolated context; "
    "only the final summary returns to the main context. Available tools "
    "(Tools: read_file, write_file, edit_file, glob, grep, bash, "
    "web_flash, deepresearch series, etc.) — it can read/write files, run CLI commands, "
    "and do web/deep research; suited for heavy work like per-page research and page "
    "generation/fixing."
)

FLASH_AVAILABLE_AGENTS: dict[str, str] = {
    "cn": FLASH_AVAILABLE_AGENTS_CN,
    "en": FLASH_AVAILABLE_AGENTS_EN,
}
