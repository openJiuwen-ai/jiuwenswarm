# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSkillProtocolRail —— 技能执行规范正文的 flash 侧修正（旧 todo 工具名 → 统一 todo）。

``SkillProtocolPromptRail``（common 层，Deep 与 flash 共用）注入 ``skill_protocol``
段时用的是 Deep 的旧 todo 工具名 ``todo_create`` / ``todo_modify(action="update", …)``；
flash 已把 todo 统一为 ``todo(action=create/list/get/modify)``（``modify`` 下用
``modify_action`` 选 update/delete/cancel/…，见 ``flash/tools/flash_todo.py``），故该段
在 flash 下会与 430「todo 纪律」段冲突（同一操作两套工具名）。

flash 技能域接管成功后，执行规范正文被拆成 730 ``skill_execution_rules``（FlashSkillRail
从 shared 协议段抽取），而 shared ``skill_protocol`` 段由 FlashSectionOrderRail 剥除；
回退原生技能域时则只剩 ``skill_protocol``。两条路径都带旧 todo 工具名，因此本 rail
同时改写这两个段名，确保任一装配路径都不漏。

本 rail 在 common rail 注入之后、审计之前，对上述段正文做**带守卫的定向改写**：
一是把 todo 工具名替换为 flash 统一 ``todo(action=…)``；二是对 todo 状态纪律做两处冲突消歧
（删掉与「立即更新、不积攒」矛盾的「可批量统一更新」句；把「todo_modify 不可独占一轮」
的例外清单补上「放弃/跳过/取消且本轮无可搭便车工作工具」），其余规范（技能加载 / 步骤声明 /
闸门 / 忠实 / 错误处理 / 用户打断等）逐字保留。改写带守卫——锚点缺失即放弃该处改写、
保留原段，避免上游改文案时静默失效。不设幂等 flag（源段每轮重注入，须每轮重写）。

优先级：只改正文、不动 priority，与 FlashSectionOrderRail（迁号/剥除）正交。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_SKILL_PROTOCOL_SECTION_NAME = "skill_protocol"
_SKILL_EXECUTION_RULES_SECTION_NAME = "skill_execution_rules"

# 改写目标段名：730 执行规范（flash 技能域接管成功后存活）与 shared skill_protocol
# （回退原生技能域时存活）。两段都改，避免「改完被剥」或「漏改回退段」。
_REWRITE_TARGET_SECTIONS: tuple[str, ...] = (
    _SKILL_EXECUTION_RULES_SECTION_NAME,
    _SKILL_PROTOCOL_SECTION_NAME,
)


def _rewrite_todo_names(text: str) -> str | None:
    """把 Deep 旧 todo 工具名改成 flash 统一 ``todo(action=…)``。

    顺序敏感：先处理示例里的 ``todo_modify(action="update", …)`` 结构（其 ``action``
    是旧子动作名，须映射成 ``modify_action``），再替换裸 ``todo_modify``，最后
    ``todo_create``。返回 ``None`` 表示旧工具名已不存在（上游已改/已移除），调用方
    应保留原段不盲改。
    """
    if "todo_create" not in text and "todo_modify" not in text:
        return None

    rewritten = text
    rewritten = rewritten.replace(
        'todo_modify(action="update"',
        'todo(action="modify", modify_action="update"',
    )
    rewritten = rewritten.replace("todo_modify", 'todo(action="modify")')
    rewritten = rewritten.replace("todo_create", 'todo(action="create")')
    return rewritten


# ── todo 状态纪律消歧（冲突清单 4/5）────────────────────────────────────────
# 4) 删「可批量统一更新」：与 430「状态变化时立即更新，不积攒」及 todo 工具描述矛盾；
# 5) 例外清单补「放弃/跳过/取消且本轮无可搭便车工作工具」：否则「禁止 todo_modify 独占
#    一轮」与「必须立即标 cancelled」在用户放弃场景互斥（多交付物加速通道要求的逐个串行，
#    由新文本末句「其它段若明确要求逐个串行推进，按该段执行」兜底）。
# 两处均以原文为锚点做替换；上游改文案时函数原样返回，由调用方告警漂移。
_BATCH_UPDATE_LINE = {
    "cn": "   - 可在连续推进多个任务后用一次 `todo_modify` 统一批量更新状态。\n",
    "en": (
        "   - You may run several tasks back-to-back and update statuses in a single "
        "batched `todo_modify`.\n"
    ),
}

_SOLE_ROUND_OLD = {
    "cn": (
        "   - 唯一例外：纯文本最终回复的前一轮。若仍有 `in_progress`，这一轮必须单独调 "
        "`todo_modify`，不要在这一轮写给用户的正文。"
    ),
    "en": (
        "   - The ONLY exception: the turn immediately before the plain-text final reply. "
        "If any item is still `in_progress`, that turn MUST call `todo_modify` alone and must "
        "not contain the user-facing answer."
    ),
}

_SOLE_ROUND_NEW = {
    "cn": (
        "   - 例外（`todo_modify` 可独占一轮）：① 纯文本最终回复的前一轮——若仍有 "
        "`in_progress`，这一轮必须单独调 `todo_modify`，不要在这一轮写给用户的正文；"
        "② 放弃、跳过或取消任务且本轮没有可搭便车的工作工具时（如用户说「不生成 PPT 了」，"
        "或第 9 条的用户打断）——必须立即用 `todo_modify` 标为 `cancelled`，允许独占一轮。"
        "搭便车是省轮次的效率约束，状态正确性优先；其它段若明确要求逐个串行推进，按该段执行。"
    ),
    "en": (
        "   - Exceptions (a standalone `todo_modify` round is allowed): (1) the turn immediately "
        "before the plain-text final reply — if any item is still `in_progress`, that turn MUST "
        "call `todo_modify` alone and must not contain the user-facing answer; (2) abandoning, "
        "skipping or cancelling work when no work tool is available to piggyback on in that round "
        "(e.g. the user says not to generate the PPT, or the user-interruption cases in rule 9) — "
        "you **must** mark it `cancelled` right away, and a standalone round is allowed. "
        "Piggybacking is an efficiency constraint to save rounds; status correctness takes "
        "precedence. If another section explicitly requires strictly serial steps, follow that "
        "section."
    ),
}

# 规则 2 的存在标记：仅用于区分「真协议正文」与合成文本，决定锚点漂移是否告警。
_RULE2_MARKERS = ("2. **必须使用 todo**", "2. **Use todo (mandatory)**")


def _has_todo_rule2(text: str) -> bool:
    return any(marker in text for marker in _RULE2_MARKERS)


def _fix_todo_status_discipline(text: str, language: str) -> tuple[str, bool]:
    """对 730 正文做 todo 状态纪律消歧，返回 ``(new_text, anchors_complete)``。

    best-effort：锚点缺失时不改动该处（不抛错、不阻断后续工具名改写）。``anchors_complete``
    为 True 表示两处锚点都已命中，供调用方区分「正常改写」与「上游漂移」。
    """
    complete = True

    batch = _BATCH_UPDATE_LINE.get(language)
    if batch and batch in text:
        text = text.replace(batch, "")
    else:
        complete = False

    old = _SOLE_ROUND_OLD.get(language)
    new = _SOLE_ROUND_NEW.get(language)
    if old and new and old in text:
        text = text.replace(old, new)
    else:
        complete = False

    return text, complete


def _strip_todo_rules(text: str, language: str) -> str | None:
    """todo 禁用时删掉「必须使用 todo」整条（规则 2）并把规则 9 的 todo 引用改中性。

    锚点守卫：规则 2 的起止锚点任一缺失即返回 ``None``（调用方保留原正文），避免
    上游改文案后盲删导致整段丢失。规则 9 的引用替换是 best-effort——替换不到就
    保留原句、不抛错。
    """
    if language == "cn":
        rule2 = "2. **必须使用 todo**："
        rule3 = "\n3. **严格顺序**"
        neutral2 = "2. **无需维护 todo**：当前环境未启用 todo 工具，本任务无需创建或更新 todo 列表。"
    else:
        rule2 = "2. **Use todo (mandatory)**:"
        rule3 = "\n3. **Strict order**"
        neutral2 = (
            "2. **No todo needed**: the todo tool is disabled in this environment; "
            "do not create or update a todo list for this task."
        )

    idx2 = text.find(rule2)
    idx3 = text.find(rule3)
    if idx2 < 0 or idx3 < 0 or idx2 >= idx3:
        return None
    text = text[:idx2] + neutral2 + text[idx3:]

    if language == "cn":
        text = text.replace(
            "先用 `todo_modify` 把所有 `in_progress`/`pending` 项标为 `cancelled`，再回应新请求。",
            "直接回应新请求。",
        )
        text = text.replace(
            "按第 2 条的适用规则处理 todo，不要沿用旧 todo。",
            "不要沿用旧任务步骤。",
        )
    else:
        text = re.sub(
            r"abandoned: first call\s+`todo_modify` to mark every "
            r"`in_progress`/`pending` item `cancelled`, then respond to the new request\.",
            "abandoned: respond to the new request directly.",
            text,
        )
        text = re.sub(
            r"handle todos under the applicable rules in item 2\s+instead of reusing the old one\.",
            "do not reuse the old task steps.",
            text,
        )
    return text


class FlashSkillProtocolRail(DeepAgentRail):
    """flash 侧修正技能执行规范正文里的旧 todo 工具名（不改 priority、不改其它规则）。"""

    # SkillProtocolPromptRail=8 注入之后、审计（FlashPromptAuditRail=2）之前。
    priority = 3
    inherit_to_subagents = False

    def __init__(self, disabled_tools_provider: Any = None) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        self._warned_guard_miss = False
        self._warned_discipline_miss = False
        self._disabled_tools_provider = disabled_tools_provider

    def _todo_disabled(self) -> bool:
        """每轮现读禁用工具集，判断 todo 是否被禁（provider 缺省时 false，保持旧行为）。"""
        provider = self._disabled_tools_provider
        if provider is None:
            return False
        try:
            return "todo" in set(provider() or ())
        except Exception:
            return False

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(ctx.agent, "system_prompt_builder", None) or self._system_prompt_builder
        if builder is None:
            return
        self._system_prompt_builder = builder
        for name in _REWRITE_TARGET_SECTIONS:
            self._rewrite_section(builder, name)

    def _rewrite_section(self, builder: Any, name: str) -> None:
        section = builder.get_section(name)
        if section is None:
            return
        content = getattr(section, "content", None)
        if not isinstance(content, dict):
            return

        todo_disabled = self._todo_disabled()
        rewritten_content: dict[str, str] = {}
        changed = False
        guard_miss = False
        for lang, text in content.items():
            if not isinstance(text, str):
                rewritten_content[lang] = text
                continue
            lang_key = "cn" if lang in ("cn", "zh") else "en"
            if todo_disabled:
                # 待办被禁用时：删掉「必须使用待办」整条并中和规则 9 残留待办引用，
                # 而非只改工具名（评审 #2）。守卫未命中返回 None → 保留原段。
                new_text = _strip_todo_rules(text, lang_key)
                fallback_text = text
            else:
                # 先做状态纪律消歧（仍按上游 ``todo_modify`` 措辞替换），再统一工具名。
                fixed_text, discipline_complete = _fix_todo_status_discipline(text, lang_key)
                if (_has_todo_rule2(text) and not discipline_complete
                        and not self._warned_discipline_miss):
                    # 上游改了状态纪律措辞：保留原句、不改坏，只告警一次提示重新对齐。
                    self._warned_discipline_miss = True
                    logger.warning(
                        "[FlashSkillProtocolRail] %s 的 todo 状态纪律锚点未命中（上游文案已变）"
                        "——保留原句，需重新对齐「删批量更新 / 补放弃例外」两处改写",
                        name,
                    )
                new_text = _rewrite_todo_names(fixed_text)
                fallback_text = fixed_text
            if new_text is None:
                guard_miss = True
                # 工具名守卫未命中时保留「消歧后的正文」，不回退掉已生效的纪律修正；
                # 消歧确有改动同样置 changed，否则 rewritten_content 会被下方
                # ``if not changed`` 整体丢弃。
                rewritten_content[lang] = fallback_text
                changed = changed or (fallback_text != text)
            else:
                rewritten_content[lang] = new_text
                changed = changed or (new_text != text)

        if guard_miss and not self._warned_guard_miss:
            # 上游已移除旧 todo 工具名/规则 2 锚点缺失（或改文案）：保留原段、不改写，且只告警一次。
            self._warned_guard_miss = True
            logger.warning(
                "[FlashSkillProtocolRail] %s 正文守卫未命中（todo_create/todo_modify 已不存在，"
                "或「必须使用 todo」锚点缺失）——保留原段，需确认是否已适配 flash 统一 todo / todo 禁用",
                name,
            )
        if not changed:
            return

        builder.add_section(PromptSection(
            name=section.name,
            content=rewritten_content,
            priority=section.priority,
            category=getattr(section, "category", None),
        ))


__all__ = [
    "FlashSkillProtocolRail",
    "_rewrite_todo_names",
    "_fix_todo_status_discipline",
    "_REWRITE_TARGET_SECTIONS",
]
