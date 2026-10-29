# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash Turbo routing guidance, independent of installed-skill retrieval."""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import PromptPriority
from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import SkillTurboPromptRail
from jiuwenswarm.server.runtime.skill_turbo.region_edit import region_edit_keyword_summary

logger = logging.getLogger(__name__)

# Stable section/tool identifiers; do not import private implementation constants.
_SECTION_NAME = "skill_turbo_guide"
_SKILL_ACCELERATION_TOOL = "skill_acceleration_exec"


class FlashTurboPromptRail(SkillTurboPromptRail):
    """Reuse Turbo discovery without restoring Deep's task-loop instructions."""

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

        # Reuse cached discovery; Flash owns its guide without changing the shared rail.
        try:
            from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                refresh_skill_acceleration_description,
            )

            skill_names = refresh_skill_acceleration_description()
        except Exception as exc:
            logger.warning("[FlashTurboPromptRail] refresh description failed: %s", exc)
            skill_names = []
        if not skill_names:
            builder.remove_section(_SECTION_NAME)
            return
        try:
            language = self._resolve_language()
            builder.add_section(PromptSection(
                name=_SECTION_NAME,
                content={language: self._build_guide_text(language, skill_names)},
                priority=self._resolve_priority(_SECTION_NAME, PromptPriority.SKILL_PROTOCOL),
            ))
        except Exception as exc:
            logger.warning("[FlashTurboPromptRail] build skill_turbo_guide section failed: %s", exc)
            builder.remove_section(_SECTION_NAME)

    def _build_guide_text(self, language: str, skill_names: list[str]) -> str:
        names = "、".join(skill_names) if language == "cn" else ", ".join(skill_names)
        keywords = region_edit_keyword_summary()
        if language == "cn":
            return f"""## 技能加速通道（skill_acceleration_exec）

当前支持：{names}。以工具描述中的支持范围为准。
- 对当前支持范围内的任务，第一个工具调用必须是 skill_acceleration_exec，包括 Word 文档和 Excel 工作簿任务。
  用户文字指定技能和前端选择技能遵循同一规则；指定当前支持的技能包含使用它的加速流程，不代表要求普通流程。
  这条规则不依赖技能检索开关，也适用于未指定技能的任务；指定技能的准确名称和任务范围必须与工具描述匹配。
- 无需先检索、调用 skill_tool 加载 SKILL.md、搜索资料或检查环境，流水线会完成这些工作。
  若已误加载当前支持的技能，但尚未生成产物且没有失败/不支持结果，仍应转入加速。
  用户明确要求的、流水线不处理的独立前置任务须先完成；指定其他技能不能因文件类型相同就被替换。
- 用户明确要求普通流程或禁用加速时遵从。
  以下排除仅适用于 PPT：模板包/指定的自定义模板文件（不含普通预置风格）、沿用已有 PPT 风格、图片转可编辑 PPT、编辑已有 PPTX 或选区（{keywords}）；图片仅作为新建演示文稿的素材不属于图片转换。
  Word / Excel 的模板填写、已有文件编辑等任务以各自支持范围为准，不套用 PPT 排除规则。
  按用户原始请求及历史上下文判断，不要把编辑任务改写为新建。
- query 忠实保留用户需求、附件路径和约束，不扩写需求；多个产物分别串行调用，等待前一任务结束后再开始下一任务。
- 成功后不要重复生成或重复发送交付总结；继续完成本请求剩余任务。
  有待办时使用实际提供的 todo 工具更新。单个产物成功不代表整个请求完成。
- 返回失败或未处理时，改用 skill_tool 的标准流程完成该任务，不重复尝试同一加速任务。
  若提示已有完成产物，继续编辑该产物，不从头重跑。
- 等待审批、用户回答或返回 __skill_turbo_hitl__ 是暂停，不是失败。
  停止后续工具调用并等待原任务续跑，不切到普通流程重新生成。
"""
        return f"""## Skill Acceleration Channel (skill_acceleration_exec)

Currently supported: {names}. Follow the supported scope in the tool description.
- For any currently supported task, the FIRST tool call MUST be skill_acceleration_exec, including Word documents and Excel workbooks.
  Naming a skill in text and selecting it in the frontend use the same rule: naming a supported skill includes its accelerated workflow, not a request for the standard workflow.
  This rule is independent of skill retrieval and also covers tasks without an explicit skill name; the exact selected skill name and task scope must match the tool description.
- Do not first search skills, load SKILL.md through skill_tool, research, or check the environment; the pipeline handles these steps.
  If a currently supported skill was mistakenly loaded but no artifact was generated and no failure/unsupported result was returned, still use acceleration.
  First complete any explicitly requested independent prerequisite the pipeline cannot handle.
  Do not replace a different explicitly selected skill merely because it handles the same file type.
- Honor requests for the standard workflow or no acceleration.
  The following exclusions apply ONLY to PPT: template packages/specified custom template files (not ordinary preset styles), reusing an existing PPT style, image-to-editable-PPT conversion, and existing PPTX or region edits ({keywords}).
  Word / Excel template filling and existing-file edits follow their own supported scope, not PPT exclusions.
  Images used only as material for a new deck are not image conversion.
  Judge the original request and history; never rewrite an edit as a new-deck task.
- Preserve the user's requirements, attachment paths, and constraints in query without inventing requirements.
  Invoke separately and serially for multiple artifacts, waiting for each task to end.
- After success, do not regenerate or repeat the delivery summary; complete remaining work.
  Update pending items using the available todo tool.
  One successful artifact does not imply the entire request is complete.
- On failure or an unhandled result, use skill_tool's standard workflow; do not retry the same acceleration task.
  If completed artifacts already exist, continue editing them without restarting generation.
- Approval, user questions, or __skill_turbo_hitl__ mean PAUSE, not failure.
  Stop further tool calls and wait for the original task to resume; do not restart through the standard workflow.
"""
