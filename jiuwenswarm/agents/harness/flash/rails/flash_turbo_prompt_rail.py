# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""复用公共 Turbo 指引，仅适配 Flash 工具可见性和统一待办参数。"""

from __future__ import annotations

import logging
from copy import copy

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import SkillTurboPromptRail

logger = logging.getLogger(__name__)

# Stable section/tool identifiers; do not import private implementation constants.
_SECTION_NAME = "skill_turbo_guide"
_SKILL_ACCELERATION_TOOL = "skill_acceleration_exec"


class FlashTurboPromptRail(SkillTurboPromptRail):
    """公共 Rail 维护技能发现与业务规则，Flash 只适配实际工具面。"""

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(ctx.agent, "system_prompt_builder", None) or self.system_prompt_builder
        if builder is None:
            return
        self.system_prompt_builder = builder
        # Never guide the model to a tool absent from this call's actual tool set.
        tools = getattr(ctx.inputs, "tools", None) or []
        if self._acceleration_disabled or not any(tool.name == _SKILL_ACCELERATION_TOOL for tool in tools):
            builder.remove_section(_SECTION_NAME)
            return

        previous = builder.get_section(_SECTION_NAME)
        try:
            await super().before_model_call(ctx)
            section = builder.get_section(_SECTION_NAME)
            # 父类构建失败会自行记录日志，但可能留下旧段落；不能沿用旧清单。
            if section is None or section is previous:
                builder.remove_section(_SECTION_NAME)
                return
            adapted = copy(section)
            adapted.content = {
                language: self._adapt_todo_calls(text)
                for language, text in section.content.items()
            }
            builder.add_section(adapted)
        except Exception as exc:
            logger.warning("[FlashTurboPromptRail] adapt skill_turbo_guide failed: %s", exc)
            builder.remove_section(_SECTION_NAME)

    @staticmethod
    def _adapt_todo_calls(text: str) -> str:
        # 只转换公共指引中的调用表达式，保留业务正文；更新操作不能只改工具名。
        update = "`todo(action=modify, modify_action=update)`"
        return (
            text.replace("`todo_create`", "`todo(action=create)`")
            .replace("`todo_modify`（action=update）", update)
            .replace("`todo_modify` (action=update)", update)
            .replace("`todo_modify`", update)
        )
