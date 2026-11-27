# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillEvolver - LLM-based experience generation with deduplication."""
from __future__ import annotations

import logging
import json
import re
from typing import Any, List, Optional

from jiuwenclaw.evolution.schema import (
    EvolutionChange,
    EvolutionEntry,
    ExperienceContext,
    EvolutionSignal,
    ExperienceTarget,
    VALID_SECTIONS,
)

logger = logging.getLogger(__name__)

_GENERATE_PROMPT = """\
你是一个 Skill 优化专家。根据对话中发现的问题信号和对话历史，为 Skill 生成演进经验。

## 输入信息

### 当前 Skill 内容
{skill_content}

### 预检测信号（规则引擎自动提取）
{signals_json}

### 对话历史
{conversation_snippet}

### 已有 description 经验
{existing_desc_summary}

### 已有 body 经验
{existing_body_summary}

## 经验来源

经验来自两个渠道，都要处理：

**渠道 A — 预检测信号**：上方「预检测信号」中列出的条目，由规则引擎自动从对话中提取，可能包含误报。

**渠道 B — 对话历史直接分析**：直接审视「对话历史」，发现规则引擎未捕获的有价值经验，包括但不限于：
- Agent 经过多次尝试/重试才成功的 workaround（说明 Skill 缺少相关指导）
- 用户含蓄的纠正或补充说明（未使用"错了""不对"等显式关键词）
- 低效的工具调用模式（如多余步骤、错误的调用顺序）
- Agent 遗漏的关键步骤（用户不得不手动补充）
- 需要特殊处理的边界情况（Skill 中未覆盖的场景）

如果对话历史中没有额外发现，不需要强制生成；如果有发现，与预检测信号的经验一起输出。

## 数量限制

最终输出的有效经验（action 为 append 的条目）**不得超过 2 条**。
如果候选经验超过 2 条，按以下优先级保留最重要的 2 条，其余标记为 skip：
1. 导致任务失败或产出错误结果的问题 > 导致效率低下但最终成功的问题
2. 高频/可复现的模式 > 单次偶发现象
3. 渠道 A（预检测信号）与渠道 B（对话分析）的发现同等对待，仅按影响程度排序

## 决策流程（对每条潜在经验按顺序执行）

### 第一步：相关性判断
判断该经验是否与 Skill 本身相关：
- 相关：问题由 Skill 的指令、脚本、示例或排查逻辑导致 -> 继续第二步
- 不相关：问题由外部因素导致（网络、环境、权限、第三方服务等）-> 输出 {{"action": "skip", "skip_reason": "irrelevant"}}

### 第二步：去重判断
对比已有演进经验（description 和 body 两个列表）：
- 实质相同：与某条已有记录内容重复 -> 输出 {{"action": "skip", "skip_reason": "duplicate"}}
- 高度相似但有增量：与某条已有记录相关但有新信息 -> 输出合并后的完整内容，并设置 "merge_target" 为目标记录 id
- 全新：与已有记录无关 -> 继续第三步

### 第三步：优先级筛选与生成
将所有通过前两步的候选经验按优先级排序，仅为排名前 2 的候选生成内容，其余输出 {{"action": "skip", "skip_reason": "low_priority"}}。
确定经验归属层（target）和章节（section），然后生成内容。

**target 判断（二选一）：**
- **description**（描述/元数据层）：涉及 Skill 适用场景判断错误、描述不准确、缺少关键词导致未被选中或误选
- **body**（正文/指令层）：涉及执行步骤、工具调用错误、操作流程、排查逻辑

**section 选择参考：**
- execution_failure / workaround 类：通常归入 Troubleshooting
- user_correction / 流程偏差类：通常归入 Instructions 或 Examples

## 内容生成规范
1. 语言一致：输出语言必须与 Skill 完全一致（中文 Skill 输出中文，英文 Skill 输出英文）
2. 标题层级：使用与 Skill 相同的标题层级（##、### 等）
3. 每条记录：1 个标题 + 2-3 个无序列表分点（- 或 *），禁止子层级
4. 每条记录只涉及一个 section 类型，不混合
5. 提取可复用的通用规则，非临时补丁（好："遇到 X 错误时，先检查 Y 再执行 Z"；差："某用户某次提到某问题"）
6. 内容必须是 Skill 中未提及的新知识，精炼简洁
7. 多个发现指向同一问题时合并为一条；不同问题分别生成
8. 有效经验（action 为 append）最多 2 条，宁缺毋滥——只保留对 Skill 改进影响最大的发现

## 输出格式
只输出以下 JSON 数组，不要其他内容（即使只有一条，也必须用数组包裹）：
[
  {{
    "action": "append | skip",
    "skip_reason": "irrelevant | duplicate | low_priority（仅 action 为 skip 时填写，否则为 null）",
    "target": "description | body",
    "section": "Instructions | Examples | Troubleshooting",
    "content": "Markdown 内容（仅 action 为 append 时填写）",
    "merge_target": "ev_xxxxxxxx 或 null"
  }}
]"""


def build_conversation_snippet(
    messages: List[dict],
    max_messages: int = 30,
    content_preview_chars: int = 300,
) -> str:
    """Build a compact conversation snippet for LLM context."""
    if not messages:
        return ""

    def _extract_text(m: dict) -> str:
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    lines: List[str] = []
    for msg in messages[-max_messages:]:
        role = msg.get("role", "unknown")
        text = _extract_text(msg).strip() or "(无文本)"
        # if len(text) > content_preview_chars:
        #     text = text[:content_preview_chars] + "..."

        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            names = [tc.get("name", "") for tc in tool_calls if isinstance(tc, dict)]
            prefix = f"[assistant] (tool_calls: {', '.join(names)})\n  "
        else:
            prefix = f"[{role}] "

        lines.append(prefix + text)
    return "\n".join(lines)


class SkillEvolver:
    """Pure logic layer: LLM-based experience generation with history dedup.

    Does NOT perform any file IO. All data is passed in as arguments and
    returned as values.
    """

    def __init__(self, llm: Any, model: str) -> None:
        self._llm = llm
        self._model = model

    async def generate_skill_experience(
        self, ctx: ExperienceContext,
    ) -> List[EvolutionEntry]:
        """Generate evolution entries via LLM, with history dedup.

        Returns:
            A list of new EvolutionEntry objects (empty list if LLM decides to skip all).
        """
        if not ctx.signals:
            return []

        conversation_snippet = build_conversation_snippet(ctx.messages)

        signals_json = json.dumps(
            [s.to_dict() for s in ctx.signals], ensure_ascii=False, indent=2
        )

        desc_summary = self._build_existing_summary(ctx.existing_desc_entries, "description")
        body_summary = self._build_existing_summary(ctx.existing_body_entries, "body")

        prompt = _GENERATE_PROMPT.format(
            skill_content=ctx.skill_content,
            signals_json=signals_json,
            conversation_snippet=(conversation_snippet or "").strip(),
            existing_desc_summary=desc_summary or "(无已有记录)",
            existing_body_summary=body_summary or "(无已有记录)",
        )

        logger.info("[SkillEvolver] calling LLM (skill=%s)", ctx.skill_name)
        try:
            response = await self._llm.invoke(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content if hasattr(response, "content") else str(response)
        except Exception as exc:
            logger.error("[SkillEvolver] LLM call failed: %s", exc)
            return []

        changes = self._parse_llm_response(raw)
        entries: List[EvolutionEntry] = []
        source = ctx.signals[0].type
        context = "; ".join(s.excerpt for s in ctx.signals)

        for change in changes:
            if change.action == "skip":
                logger.info(
                    "[SkillEvolver] LLM decided to skip (reason=%s)",
                    change.skip_reason or "unknown",
                )
                continue
            if not change.content.strip():
                logger.info("[SkillEvolver] LLM returned empty content, skipping")
                continue
            entry = EvolutionEntry.make(source=source, context=context, change=change)
            logger.info(
                "[SkillEvolver] generated entry %s -> [%s] target=%s merge_target=%s",
                entry.id,
                change.section,
                change.target.value,
                change.merge_target,
            )
            entries.append(entry)
        return entries[:2]

    def update_llm(self, llm: Any, model: str) -> None:
        self._llm = llm
        self._model = model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_existing_summary(entries: List[EvolutionEntry], label: str = "") -> str:
        if not entries:
            return ""
        lines: List[str] = []
        for e in entries:
            prefix = f"[{label}] " if label else ""
            lines.append(f"- {prefix}[{e.id}] [{e.change.section}] {e.change.content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_llm_response(raw: str) -> List[EvolutionChange]:
        """从 LLM 返回里解析出 EvolutionChange 列表（支持数组和单对象两种格式）。"""
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)
        raw = raw.strip()

        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 先尝试匹配数组
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            # 回退：尝试匹配单个对象
            if data is None:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        pass
            if data is None:
                logger.warning(
                    "[SkillEvolver] cannot parse LLM response as JSON: %s",
                    raw[:200],
                )
                return []

        items = data if isinstance(data, list) else [data]
        changes: List[EvolutionChange] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            change = SkillEvolver._parse_single_change(item)
            if change is not None:
                changes.append(change)
        return changes

    @staticmethod
    def _parse_single_change(data: dict) -> Optional[EvolutionChange]:
        """从单个 JSON 对象解析出 EvolutionChange。"""
        action = data.get("action", "append")
        if action == "skip":
            skip_reason = data.get("skip_reason", "unknown")
            return EvolutionChange(
                section="", action="skip", content="",
                skip_reason=skip_reason,
            )

        section = data.get("section", "Troubleshooting")
        if section not in VALID_SECTIONS:
            section = "Troubleshooting"

        raw_target = data.get("target", "body")
        try:
            target = ExperienceTarget(raw_target)
        except ValueError:
            target = ExperienceTarget.BODY

        merge_target = data.get("merge_target")
        if merge_target == "null" or merge_target is None:
            merge_target = None

        return EvolutionChange(
            section=section,
            action="append",
            content=data.get("content", ""),
            target=target,
            merge_target=merge_target,
        )
