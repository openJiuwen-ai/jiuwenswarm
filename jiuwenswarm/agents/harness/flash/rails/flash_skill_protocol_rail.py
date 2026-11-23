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
只替换 todo 工具名，不动其余规范（技能加载 / 步骤声明 / 闸门 / 忠实 / 错误处理 /
用户打断等规则逐字保留）。改写带守卫——旧工具名缺失即放弃改写、保留原段，避免上游
改文案时静默失效。不设幂等 flag（源段每轮重注入，须每轮重写）。

优先级：只改正文、不动 priority，与 FlashSectionOrderRail（迁号/剥除）正交。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
    _strip_todo_rules,
)

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


class FlashSkillProtocolRail(DeepAgentRail):
    """flash 侧修正技能执行规范正文里的旧 todo 工具名（不改 priority、不改其它规则）。"""

    # SkillProtocolPromptRail=8 注入之后、审计（FlashPromptAuditRail=2）之前。
    priority = 3
    inherit_to_subagents = False

    def __init__(self, disabled_tools_provider: Any = None) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        self._warned_guard_miss = False
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
                # todo 被禁：删掉「必须使用 todo」整条并中和规则 9 残留 todo 引用，
                # 而非只改工具名（评审 #2）。守卫未命中返回 None → 保留原段。
                new_text = _strip_todo_rules(text, lang_key)
            else:
                new_text = _rewrite_todo_names(text)
            if new_text is None:
                guard_miss = True
                rewritten_content[lang] = text
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


__all__ = ["FlashSkillProtocolRail", "_rewrite_todo_names", "_REWRITE_TARGET_SECTIONS"]
