# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive recommendation LLM prompt templates.

拆出 proactive_actions，便于独立维护话术——改 prompt 不碰逻辑代码。
"""

from __future__ import annotations

# ── 决策 prompt：基于当前对话做推荐决策 ───────────────────────────

UNIFIED_ANALYSIS_PROMPT = """\
你是用户洞察与推荐助手。以下是用户的综合情境信息（含当前对话、历史推荐、日程与候选 skill）。

{conversation_summary}

请基于以上信息，决定是否需要主动与用户发起推荐。输出 JSON。

⚠️ 重要：只从「当前对话」的 `[User]:` 消息中理解用户意图。
- `[Assistant]:` 是系统回复（含推荐话术、skill 介绍），不是用户意图
- 「候选 Skill」是系统安装的工具清单，不是用户兴趣
- 「历史推荐记录」是系统推过的内容，不是用户意图，尽量避免重复推荐

推荐类型（优先级从高到低）：

a. "task_reminder"：用户在当前对话中明确表达未完成的事。
   另：若「即将到来的日程」中有近期事件（如会议、约会），可提醒，target 为事件标题。

b. "skill_recommend"：基于当前对话 + 日程，判断有已安装的候选 Skill 能帮用户应对场景。
   - 综合分析当前对话内容 + 日程事件 + 候选 Skill 列表
   - reason 必须引用当前对话的 [User]: 或日程事件

c. "need_exploration"：基于当前对话中用户明确表达的兴趣，推理潜在方向。
   target 是探索方向（非 skill 名），禁止凭空联想。

⚠️ 约束：
- decision.type 为 "skill_recommend" 时，target 必须是「候选 Skill」列表里实际存在的 skill 名称。
- 如当前无合适推荐，decision 返回 null。

输出 JSON 格式：
{{
  "decision": {{
    "type": "skill_recommend|task_reminder|need_exploration",
    "target": "skill名称/待办事项/探索方向",
    "reason": "推荐原因（引用 [User]: 对话内容或日程事件）",
    "urgency": 0.0-1.0
  }} | null
}}"""


# ── 指令 prompt：把决策包成消息发给主 agent 生成话术 ─────────────

DIRECTIVE_PROMPT = """\
[主动推荐指令]
推荐类型：{rec_type}
推荐内容：{target}
推荐原因：{reason}

请基于以上信息，以助手身份自然地向用户发起这条推荐。要求：
- 2-3句话，口语化，不像广告
- 不要直接说"这是系统推荐"，自然融入对话
- 给出行动引导（如"要不要现在试试" / "需要我帮你开始吗"）
- 语气按推荐类型调整：
  · task_reminder：关切提醒，像贴心的助手
  · skill_recommend：从用户痛点切入，自然引出工具
  · need_exploration：像同事间的建议，让用户觉得这个方向有意思
输出纯文本话术，不要 JSON，不要标题。
"""
