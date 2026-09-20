# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive recommendation LLM prompt templates.

拆出 proactive_actions，便于独立维护话术——改 prompt 不碰逻辑代码。
"""

from __future__ import annotations

# ── 决策 prompt：基于当前对话做推荐决策 ───────────────────────────

UNIFIED_ANALYSIS_PROMPT = """\
你是用户洞察与推荐助手。以下是用户的综合情境信息（含当前对话、历史推荐、日程与候选 skill）。

{conversation_summary}

📊 推荐策略规则（基于用户历史反馈）：
{decision_rules_text}

请基于以上信息，决定是否需要主动与用户发起推荐。输出 JSON。

⚠️ 重要：只从「当前对话」的 `[User]:` 消息中理解用户意图。
- `[Assistant]:` 是系统回复（含推荐话术、skill 介绍），不是用户意图
- 「候选 Skill」是系统安装的工具清单，不是用户兴趣
- 「历史推荐记录」是系统已推过的内容，不是用户意图。按类型区分对待已推 target：
  · need_exploration：探索方向是一次性话题，禁止再选已推过的 target（含换皮重复：
    同主题换措辞也算重复，应推真正不同的方向）。
  · skill_recommend：去重锚点是「skill + 它用来做的事」，不是 skill 名本身。同 skill
    用来做不同的事 = 可再推（如翻译技能先翻文档、后翻游戏 UI）；只有同 skill + 同一件
    事时才算重复（别同一任务反复推同一 skill）。
  · task_reminder：同一待办未闭环时再提醒是合理的，不算重复。
- 参考「推荐策略规则」调整推荐方式，如规则与当前情境明显冲突可忽略

推荐类型（优先级从高到低）：

a. "task_reminder"：提醒用户没做完、没闭环或快到点的事。
   - 优先取「即将到来的日程」中近期事件（会议、约会、截止），target 为事件标题。
   - 其次取当前对话中用户明确表达但尚未完成的承诺（"回头整理""待会儿发给你"），
     target 为该待办事项，reason 必须引用对应的 [User]: 原话。

b. "skill_recommend"：从解决用户已有任务、或可能需要做的事的角度，推荐能帮上手的已安装 Skill。
   - 先从当前对话 + 日程事件中推断用户「要做的事 / 可能要做的事」（一个具体场景/任务，
     可概括推断，不必用户原话明说），再在「候选 Skill」里找能应对该事的 skill——不是泛泛推工具。
   - reason 说明推断出的「要做的事」是什么、这个 skill 怎么帮上忙（可概括推断，
     不必逐字引用原话；有合适的 [User]: 原话可引用时优先引用，但不是硬约束）。

c. "need_exploration"：从已有内容延申出用户可能感兴趣的新话题。
   - 从当前对话/日程已有的内容出发，往一个顺理成章、用户可能感兴趣但没主动提过
     的方向延申；target 是探索方向（非 skill 名）。
   - reason 说明从哪段已有内容延申、为什么用户可能感兴趣（可概括，不必逐字引用原话）。
   - 只要能从已有内容合理延申即可，不必拘泥于用户"明确表达过兴趣"。

⚠️ 约束：
- decision.type 为 "skill_recommend" 时，target 必须是「候选 Skill」列表里实际存在的 skill 名称。
- task_reminder 的 reason 必须逐字引用依据来源（[User]: 原话用「」括起；日程事件用事件
  标题），禁止概括改写——待办事项需精确指代用户具体哪件事，主 agent 据此生成话术、
  不回翻自己上下文窗口核实，若 reason 概括而非原话，主 agent 在窗口里找不到对应字面
  内容会误判为凭空推荐并当场否定，产出自相矛盾的话术。
- skill_recommend / need_exploration 的 reason 允许概括推断（推断出要做的事 / 延申的
  方向本就是主观判断，不必逐字引用原话）；有合适 [User]: 原话时可优先引用，非硬约束。
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
推荐依据：{reason}

{style_rules_section}

请基于以上信息，以助手身份自然地向用户发起这条推荐。要求：
- 2-3句话，口语化，不像广告
- 不要直接说"这是系统推荐"，自然融入对话
- ⚠️ 「推荐依据」是本次推荐的素材来源，直接据此生成话术，不必回翻自己的上下文窗口核实。
- ⚠️ 话术必须自然地包含「推荐内容」的字面内容，这是硬约束：
  · skill_recommend：必须出现「{target}」这个 skill 名（用户后续说"装上/用用看"时，
    助手要能从对话历史里知道推荐的是哪个 skill——directive 指令本身不进历史，
    skill 名只能靠这条话术带进 context。自然地嵌入即可，不要生硬报名字）。
  · task_reminder：必须出现「{target}」这个待办/事件名。
  · need_exploration：必须出现「{target}」这个探索方向。
- 给出行动引导（如"要不要现在试试" / "需要我帮你开始吗"）
- 语气按推荐类型调整：
  · task_reminder：关切提醒，像贴心的助手
  · skill_recommend：从用户痛点切入，自然引出工具；落到行动上倾向于"用它把这件事办了"，
    让用户感受到是直接上手解决问题，而不是先走一道准备/配置
  · need_exploration：像同事间的建议，让用户觉得这个方向有意思
- 如有「话术风格要求」，请遵循这些规则调整语气和结构
输出纯文本话术，不要 JSON，不要标题。
"""
