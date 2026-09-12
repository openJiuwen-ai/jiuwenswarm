# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gradient updater for proactive recommendation optimization.

Uses Text Gradient approach: user feedback → strategy gradient rules (natural language).
Unified update flow: new feedback + existing gradients → updated gradients.
"""

from __future__ import annotations

import json
import logging
import re
import time

from typing import Any

logger = logging.getLogger(__name__)


# ── Gradient Update Prompt ──────────────────────────────────────────

GRADIENT_UPDATE_PROMPT = """\
你是推荐策略分析师。根据用户反馈，更新推荐策略规则。

【用户反馈】
{feedbacks_text}

【已有策略规则】
{existing_rules_text}

请分析用户反馈，提取改进建议并更新策略规则。

⚠️ 反馈类型与规则生成的对应关系：

1. 显式反馈（点赞/点踩）：
   - 点赞表示用户对该推荐话题感兴趣
   - 点踩表示用户对该推荐话题不感兴趣
   - 只能生成 target 类型规则（关于"推什么/不推什么"）
   - 示例：用户点赞"翻译技能" → "用户对翻译类话题感兴趣"
   - 示例：用户点踩"日程提醒" → "避免推荐日程类话题"

2. 隐式反馈（用户文本回复）：
   - 用户明确表达了意见或建议
   - 可以生成任意类型规则（target/relation/tone/structure）
   - 必须基于用户明确表达的内容，不要猜测

⚠️ 规则分类：
- target: 关于"推什么"（如"不要推翻译类"、"优先推荐效率类"）
- relation: 关于"怎么关联"（如"关联日程要有实际关联"、"引用用户原话"）
- tone: 关于"语气风格"（如"简洁克制"、"不要过度热情"）
- structure: 关于"话术结构"（如"1-2句话"、"不要解释功能"）

每条规则一句话，不超过25字。

⚠️ 避免语义重复：如果新反馈与已有规则相关，优先 revise 已有规则，而非 add 新规则。

输出 JSON：
{{
  "operations": [
    {{"action": "add", "rule": "新规则", "category": "target|relation|tone|structure"}},
    {{"action": "revise", "gradient_id": "g_xxx", "rule": "修正后的规则", "category": "target|relation|tone|structure"}},
    {{"action": "drop", "gradient_id": "g_yyy"}}
  ]
}}

字段说明：
- action: "add" | "revise" | "drop"
- rule: 规则内容（add/revise 必填）
- category: 规则分类（add/revise 必填）
- gradient_id: 要修改/删除的规则ID（revise/drop 必填）

如果没有需要更新的，返回 {{"operations": []}}。
"""


# ── Helper functions ────────────────────────────────────────────────

def _render_feedbacks(feedbacks: list[dict]) -> str:
    """Render feedback records for prompt."""
    if not feedbacks:
        return "（无反馈）"

    lines = []
    for i, fb in enumerate(feedbacks, 1):
        feedback_type = fb.get("feedback_type", "unknown")
        rec_content = fb.get("rec_content", "")
        user_reply = fb.get("user_reply", "")

        line = f"{i}. 反馈类型: {feedback_type}"
        if rec_content:
            line += f"\n   推荐内容: {rec_content}"
        if user_reply:
            line += f"\n   用户回复: {user_reply}"

        lines.append(line)

    return "\n".join(lines)


def _render_gradients(gradients: list[dict]) -> str:
    """Render existing gradients for prompt."""
    if not gradients:
        return "（暂无）"

    lines = []
    for g in gradients:
        gid = g.get("gradient_id", "unknown")
        category = g.get("category", "unknown")
        rule = g.get("rule", "")
        lines.append(f"- [{gid}] ({category}) {rule}")

    return "\n".join(lines)


def _extract_output_text(result: Any) -> str:
    """Extract text output from agent invoke result."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        out = result.get("output")
        if isinstance(out, str):
            return out
        if isinstance(out, list):
            return "\n".join(s for s in out if isinstance(s, str))
        return ""
    return ""


def _extract_json_from_response(text: str) -> str:
    """Extract JSON from LLM response (handles markdown code blocks).

    fallback 用 json.JSONDecoder().raw_decode 而非贪婪正则 r'\\{.*\\}'：贪婪正则从
    首个 '{' 匹配到末个 '}'，若 LLM 在 JSON 后附了含 '}' 的文本会截到无效 JSON 致
    json.loads 失败。raw_decode 从首个 '{' 解析出一段合法 JSON 即止，忽略尾部文本。
    """
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    # raw_decode 从首个 '{' 解析合法 JSON 段，越界文本忽略；找不到则返回空。
    start = text.find("{")
    if start == -1:
        return ""
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return json.dumps(obj, ensure_ascii=False)
    except ValueError:
        # raw_decode 解析失败抛 ValueError（json.JSONDecodeError 是其子类，父类即覆盖，
        # 不重复捕获父子同类异常，避免 G.ERR.09）。
        return ""


# ── Core functions ──────────────────────────────────────────────────

def apply_operations(gradients: list[dict], operations: list[dict]) -> list[dict]:
    """Apply operations to gradients, return updated list.

    Operations:
    - add: create new gradient
    - revise: modify existing gradient
    - drop: remove existing gradient
    """
    gradient_map = {g["gradient_id"]: g for g in gradients}

    for op in operations:
        action = op.get("action")

        if action == "add":
            # 过滤空 rule：LLM 可能返回 {"action":"add","rule":"","category":"target"}，
            # 空规则会被 render 成 "- [target] " 注入 prompt，浪费 context 且可能误导。
            rule_text = (op.get("rule", "") or "").strip()
            if not rule_text:
                logger.debug("[GradientUpdater] add op with empty rule, skipped")
                continue
            gid = f"g_{int(time.time() * 1000)}_{len(gradient_map)}"
            gradient_map[gid] = {
                "gradient_id": gid,
                "rule": rule_text,
                "category": op.get("category", "tone"),
            }

        elif action == "revise":
            gid = op.get("gradient_id")
            if gid and gid in gradient_map:
                # revise 后挪到末尾：dict 删原 key 再重新插入 = 插入序里排到最后。
                # 这样"最近被新反馈更新过的规则"反映新近度——配合存储/喂 LLM 截断留
                # 最新（[-N:]），被 revise 的规则不会被"留最早"逻辑误淘汰，且优先被喂。
                revised = gradient_map.pop(gid)
                revised["rule"] = op.get("rule", revised["rule"])
                revised["category"] = op.get("category", revised["category"])
                gradient_map[gid] = revised

        elif action == "drop":
            gid = op.get("gradient_id")
            if gid and gid in gradient_map:
                del gradient_map[gid]

    return list(gradient_map.values())


async def update_gradients(
    feedbacks: list[dict],
    existing_gradients: list[dict],
    proactive_agent: Any,
) -> list[dict]:
    """Unified gradient update flow.

    When existing_gradients is empty → first-order gradient (extract from feedback)
    When existing_gradients is non-empty → second-order gradient (revise existing)

    Args:
        feedbacks: List of feedback records from buffer
        existing_gradients: Current strategy gradients
        proactive_agent: LLM agent for gradient generation

    Returns:
        Updated list of gradients
    """
    if not feedbacks:
        return existing_gradients

    # 分离显式和隐式反馈
    explicit_feedbacks = [fb for fb in feedbacks if fb.get("feedback_type") in ("explicit_like", "explicit_dislike")]
    implicit_feedbacks = [fb for fb in feedbacks if fb.get("user_reply")]

    # 处理显式反馈：只生成 target 类型规则
    if explicit_feedbacks:
        existing_gradients = await _update_gradients_from_explicit_feedback(
            explicit_feedbacks, existing_gradients, proactive_agent
        )

    # 处理隐式反馈：可以生成任意类型规则
    if implicit_feedbacks:
        existing_gradients = await _update_gradients_from_implicit_feedback(
            implicit_feedbacks, existing_gradients, proactive_agent
        )

    return existing_gradients


async def _update_gradients_from_explicit_feedback(
    feedbacks: list[dict],
    existing_gradients: list[dict],
    proactive_agent: Any,
) -> list[dict]:
    """从显式反馈生成 target 类型规则。"""
    feedbacks_text = _render_feedbacks(feedbacks)
    existing_text = _render_gradients(existing_gradients)

    # 显式反馈专用 prompt，只允许生成 target 类型规则。
    # 泛化保守：单次反馈只记具体 target + 场景（从 rec_content 提取），不泛化成大类；
    # 只有已有多条同类反馈时才 revise 成类偏好。避免一条点踩 → 整大类被压抑误伤其他场景。
    prompt = f"""\
你是推荐策略分析师。根据用户的点赞/点踩反馈，更新推荐策略规则。

【用户反馈】
{feedbacks_text}

【已有策略规则】
{existing_text}

⚠️ 重要约束：
- 点赞表示用户对该推荐感兴趣，点踩表示对该推荐不感兴趣
- 只能生成 target 类型规则（关于"推什么/不推什么"）
- 不要生成 tone/structure/relation 类型的规则
- 不要猜测用户为什么点赞/点踩

⚠️ 泛化保守（关键）：
- 判断用户是对"这个具体推荐场景"不感兴趣，还是对"这类 skill/话题"整体不感兴趣——
  优先看「推荐内容」(rec_content) 里的具体场景（如"为做 NBA PPT 推写作技能"），
  按具体场景记规则，不要仅凭 skill 名泛化成大类。
- 单次反馈：只记具体 target + 场景，例如：
  · 用户点踩"general-writing（NBA PPT 文案场景）" → "为 NBA PPT 推写作技能不受欢迎"
    （不泛化成"避免文案生成类话题"——那会误伤其他需要文案的场景）
  · 用户点赞"翻译技能（翻外语文档场景）" → "翻译技能用于翻文档受欢迎"
- 仅当「已有策略规则」里已有多条同类反馈的记录时，才 revise 成类偏好：
  例如已有多条"不同文案类技能点踩" → 才合并成"避免文案生成类话题"

示例：
- 单次反馈记具体：用户点赞"翻译技能" → "用户对翻译技能用于翻文档感兴趣"（不泛化成"翻译类"）
- 多次同类才泛化：已有"翻译技能点踩"+"其他翻译技能点踩" → revise 成"避免翻译类话题"

输出 JSON：
{{
  "operations": [
    {{"action": "add", "rule": "新规则", "category": "target"}},
    {{"action": "revise", "gradient_id": "g_xxx", "rule": "修正后的规则", "category": "target"}},
    {{"action": "drop", "gradient_id": "g_yyy"}}
  ]
}}

如果没有需要更新的，返回 {{"operations": []}}。
"""

    conv_id = f"gradient_update_explicit_{int(time.time() * 1000)}"
    try:
        result = await proactive_agent.invoke({
            "query": prompt,
            "conversation_id": conv_id,
        })
        content = _extract_output_text(result)
        json_str = _extract_json_from_response(content)
        if not json_str:
            logger.warning("[GradientUpdater] no JSON in response for explicit feedback")
            return existing_gradients

        data = json.loads(json_str)
        operations = data.get("operations", [])

        # 强制过滤：只保留 target 类型的操作
        operations = [op for op in operations if op.get("category") == "target" or op.get("action") == "drop"]

        if not operations:
            logger.info("[GradientUpdater] no target operations from explicit feedback")
            return existing_gradients

        updated = apply_operations(existing_gradients, operations)
        logger.info("[GradientUpdater] applied %d operations from explicit feedback", len(operations))
        return updated

    except Exception as exc:
        logger.warning("[GradientUpdater] explicit feedback update failed: %s", exc, exc_info=True)
        return existing_gradients
    finally:
        try:
            from openjiuwen.core.session.checkpointer import CheckpointerFactory
            await CheckpointerFactory.get_checkpointer().release(conv_id)
        except Exception as exc:
            # 释放 checkpointer 是清理动作，失败本就该静默——裸 pass 被 lint 拦，
            # 改 debug 记录，吞没的同时留排查线索。
            logger.debug("[GradientUpdater] checkpoint release failed: %s", exc)


async def _update_gradients_from_implicit_feedback(
    feedbacks: list[dict],
    existing_gradients: list[dict],
    proactive_agent: Any,
) -> list[dict]:
    """从隐式反馈生成任意类型规则。"""
    feedbacks_text = _render_feedbacks(feedbacks)
    existing_text = _render_gradients(existing_gradients)

    # 隐式反馈专用 prompt，可以生成任意类型规则。
    # 泛化保守 + 读场景：从 rec_content 提取具体场景，单次反馈只记具体，多次同类才泛化。
    # 无关反馈（用户聊别的、和推荐无关）返回空 operations——配合砍掉的时间窗，相关性由模型判断。
    prompt = f"""\
你是推荐策略分析师。根据用户的文本回复，更新推荐策略规则。

【用户反馈】
{feedbacks_text}

【已有策略规则】
{existing_text}

⚠️ 重要约束：
- 【用户反馈】里的"反馈类型: implicit"表示这是用户的文本回复原文，情感未预分类
  （此前用关键词规则分类误判率高，已移除）。你需先从 user_reply 原文判断其情感
  倾向（正面/负面/中性），再决定是否生成规则。中性回复返回空 operations。
- 必须基于用户明确表达的内容生成规则
- 不要猜测用户的意图
- 可以生成任意类型规则（target/relation/tone/structure）

⚠️ 相关性判断（配合砍掉的时间窗）：
- user_reply 可能和推荐无关（用户聊到别的话题）。若回复与「推荐内容」无关联，
  返回空 operations，不产生梯度——不要硬凑关联。

⚠️ 泛化保守（关键）：
- 优先看「推荐内容」(rec_content) 里的具体场景，按具体场景记规则，不要仅凭
  skill 名/话题泛化成大类。
- 单次反馈只记具体 target + 场景，例如用户对"为 NBA PPT 推的写作技能"回复
  "不需要" → "为 NBA PPT 推写作技能不受欢迎"（不泛化成"避免文案类"）。
- 仅当「已有策略规则」里已有多条同类反馈时，才 revise 成类偏好。

示例：
- 用户说"简洁点" → 生成规则："语气简洁克制"（tone 类型）
- 用户说"不要推翻译类" → 生成规则："避免推荐翻译类话题"（target 类型，因用户明确说了"翻译类"）
- 用户回复和推荐无关（如聊到别的话题）→ 返回空 operations

输出 JSON：
{{
  "operations": [
    {{"action": "add", "rule": "新规则", "category": "target|relation|tone|structure"}},
    {{"action": "revise", "gradient_id": "g_xxx", "rule": "修正后的规则", "category": "target|relation|tone|structure"}},
    {{"action": "drop", "gradient_id": "g_yyy"}}
  ]
}}

如果没有需要更新的，返回 {{"operations": []}}。
"""

    conv_id = f"gradient_update_implicit_{int(time.time() * 1000)}"
    try:
        result = await proactive_agent.invoke({
            "query": prompt,
            "conversation_id": conv_id,
        })
        content = _extract_output_text(result)
        json_str = _extract_json_from_response(content)
        if not json_str:
            logger.warning("[GradientUpdater] no JSON in response for implicit feedback")
            return existing_gradients

        data = json.loads(json_str)
        operations = data.get("operations", [])

        if not operations:
            logger.info("[GradientUpdater] no operations from implicit feedback")
            return existing_gradients

        updated = apply_operations(existing_gradients, operations)
        logger.info("[GradientUpdater] applied %d operations from implicit feedback", len(operations))
        return updated

    except Exception as exc:
        logger.warning("[GradientUpdater] implicit feedback update failed: %s", exc, exc_info=True)
        return existing_gradients
    finally:
        try:
            from openjiuwen.core.session.checkpointer import CheckpointerFactory
            await CheckpointerFactory.get_checkpointer().release(conv_id)
        except Exception as exc:
            # 释放 checkpointer 是清理动作，失败本就该静默——裸 pass 被 lint 拦，
            # 改 debug 记录，吞没的同时留排查线索。
            logger.debug("[GradientUpdater] checkpoint release failed: %s", exc)


# ── Gradient Attribution ────────────────────────────────────────────

DECISION_CATEGORIES = {"target", "relation"}
STYLE_CATEGORIES = {"tone", "structure"}


def attribute_gradients(gradients: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attribute gradients to decision layer and style layer.

    Args:
        gradients: All strategy gradients

    Returns:
        (decision_gradients, style_gradients): Attributed gradient groups
    """
    decision_gradients = []
    style_gradients = []

    for g in gradients:
        category = g.get("category", "")
        if category in DECISION_CATEGORIES:
            decision_gradients.append(g)
        elif category in STYLE_CATEGORIES:
            style_gradients.append(g)
        else:
            # Unknown category defaults to decision layer
            decision_gradients.append(g)

    return decision_gradients, style_gradients


def render_decision_rules(gradients: list[dict]) -> str:
    """Render decision layer rules for UNIFIED_ANALYSIS_PROMPT."""
    decision_g, _ = attribute_gradients(gradients)

    if not decision_g:
        return "（暂无）"

    lines = []
    # 留最新 10 条喂决策 LLM：与存储留最新（[-20:]）方向一致，反映最近反馈学到的偏好。
    for g in decision_g[-10:]:
        lines.append(f"- [{g.get('category', 'unknown')}] {g.get('rule', '')}")

    return "\n".join(lines)


def render_style_rules(gradients: list[dict]) -> str:
    """Render style layer rules for DIRECTIVE_PROMPT."""
    _, style_g = attribute_gradients(gradients)

    if not style_g:
        return ""

    lines = ["【话术风格要求】（基于用户历史反馈）"]
    # 留最新 10 条喂话术 LLM（与决策层一致，反映最近反馈学到的偏好）。
    for g in style_g[-10:]:
        lines.append(f"- {g.get('rule', '')}")

    return "\n".join(lines)
