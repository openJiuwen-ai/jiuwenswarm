# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboPromptRail — 注入 skill_acceleration_exec 使用指南。

在每次 LLM 调用前向 system prompt 注入 skill_turbo_guide 段，
指导 LLM 何时使用 skill_acceleration_exec 加速通道以及失败后的降级策略。

清单动态化（请求期）：外部 turbo 技能目录的可见性依赖请求级上下文
（session 绑定目录 / env），sidecar 启动（模块导入）时不可见，工具描述
若仅依赖导入期固化值会恒为"暂无"。本 rail 在每次模型调用前调用
``refresh_skill_acceleration_description`` 重建 ToolCard.description，
并**用同一次扫描结果**生成指南中的技能清单——两处清单永远同源一致，
不出现"描述说暂无、指南却举例三技能"的矛盾指令。

清单为空（当前请求上下文真实无可见加速技能）时**整段不注入**
（对齐 ``acceleration_disabled`` 先例：加速通道不可用即不引导，避免
强制指令驱使 LLM 调用空清单工具后空跑一轮降级往返）。

仅在 config.react.skill_turbo.enabled = true 时生效。
当构造参数 ``acceleration_disabled=True``（调用方根据 disabled_tools 判定）
时不注入。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import PromptPriority

logger = logging.getLogger(__name__)

_SECTION_NAME = "skill_turbo_guide"
_SKILL_ACCELERATION_TOOL = "skill_acceleration_exec"


def _build_skill_turbo_guide_text(language: str, skill_names: list[str]) -> str:
    """生成技能加速指南文本；skill_names 为当前请求可见的技能清单（与工具描述同源）。"""
    if language == "cn":
        names = "、".join(skill_names)
        return f"""## 技能加速通道（skill_acceleration_exec）

⚠️ **默认优先**：当用户意图匹配 `skill_acceleration_exec`（当前支持的技能清单以该工具描述中的"当前内部支持"为准，当前：{names}），你的**第一个工具调用必须是 `skill_acceleration_exec`**——它内部会自行完成规划+研究+生成+推送全流程。**唯一例外**：用户**显式**要求先执行某项 `skill_acceleration_exec` 不会自行处理的前置操作，则必须先完成该操作再调用 `skill_acceleration_exec`。搜索、读文件等研究类操作它内部会自己完成，**不构成**例外；其他技能类操作（如加载独立技能流程）它不会自行处理，**构成**例外。

若你已先调用了 `skill_tool` 加载了对应技能正文（即**不属于**上述例外，只是误触），**仍须立即调用 `skill_acceleration_exec`**——不要因"已经加载了 SKILL.md"或"已经做了研究"而放弃加速通道。

若 `skill_acceleration_exec` 返回成功（产物已生成），**禁止**再用 `skill_tool` 重复同一任务——直接向用户总结结果即可。若 `skill_acceleration_exec` 返回失败或未处理，**必须**继续用 `skill_tool` 加载对应技能走标准流程完成用户任务。

🚫 **选区/编辑已有 PPT 排除（重要例外，仅适用于 PPT 类任务）**：当用户消息出现"PPT选区/选区原文/选区类型/选区位置/选区容器/选区 class/修改要求"等选区字段，或意图是**编辑、修改已有 PPT 文件的局部内容**（如改字体颜色、改某段文案、调某页样式、替换某区域）而非从零生成整套演示文稿时，**禁止调用 `skill_acceleration_exec`**——它只会从流水线 Stage 1 重新生成全新 PPT，无法复用已有文件做局部修改。此类请求请改用 `skill_tool` 加载 pptx-craft 标准流程（支持编辑已有 PPT），或直接用 `edit_file`/读写 pptx 的普通工具完成局部修改。本条仅约束 PPT 局部编辑类请求，不适用于从零生成新产物的任务。
"""
    names = ", ".join(skill_names)
    return f"""## Skill Acceleration Channel (skill_acceleration_exec)

⚠️ **Default priority**: When the user's intent matches `skill_acceleration_exec` (see the "当前内部支持 / currently supported" list in that tool's description; currently: {names}), your **FIRST tool call MUST be `skill_acceleration_exec`** — it handles planning + research + generation + delivery internally. **Only exception**: the user **explicitly** asks to first perform a preceding action that `skill_acceleration_exec` does not handle internally; then you must complete that action before calling `skill_acceleration_exec`. Research-style actions like `web_search` and file reading are handled internally — they do **NOT** constitute an exception; other skill-type actions (e.g. loading a separate skill flow) are not handled internally and **DO** constitute an exception.

If you have already mistakenly called `skill_tool` to load the corresponding skill body (i.e. this does **NOT** fall under the exception above - it was just a misfire), **you MUST still call `skill_acceleration_exec` immediately** - do NOT abandon the acceleration channel because "SKILL.md is already loaded" or "research is already done."

If `skill_acceleration_exec` returns success (the artifact is already generated), you are **forbidden** from calling `skill_tool` again for the same task — just summarize the result to the user. If `skill_acceleration_exec` returns failure or is not handled, you **MUST** fall back to `skill_tool` to load the corresponding skill and complete the user's task via the standard flow.

🚫 **PPT region/edit-existing exclusion (critical exception, PPT tasks only)**: When the user message contains selection fields such as "PPT选区/选区原文/选区类型/选区位置/选区容器/选区 class/修改要求", or the intent is to **edit or modify a local part of an existing PPT file** (e.g. change font color, rewrite a paragraph, restyle a slide, replace a region) rather than generating a full deck from scratch, you are **FORBIDDEN from calling `skill_acceleration_exec`** — it only regenerates a brand-new PPT from pipeline Stage 1 and cannot reuse an existing file for local edits. For such requests, use `skill_tool` to load the pptx-craft standard flow (which supports editing existing PPTs), or directly use `edit_file` / pptx read-write tools to make the local change. This clause applies ONLY to local edits of existing PPTs, not to creating new artifacts from scratch.
"""


def _refresh_description_and_list_skills() -> list[str]:
    """请求期刷新工具描述并返回当前技能清单（见模块 docstring「清单动态化」）。

    延迟 import：skill_turbo_tools 模块导入伴随 @tool 注册等初始化，
    且与本 rail 无循环依赖；refresh 自身已兜异常（失败返回最近已知清单）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        refresh_skill_acceleration_description,
    )

    return refresh_skill_acceleration_description()


class SkillTurboPromptRail(DeepAgentRail):
    """Inject skill_acceleration_exec usage guide before each model call.

    仅在 config.react.skill_turbo.enabled = true 时注入提示词。
    Callers that honor ``react.disabled_tools`` must pass
    ``acceleration_disabled=True`` when ``skill_acceleration_exec`` is blocked;
    this rail does not scan sibling rails at runtime.
    """

    priority = 8

    def __init__(self, *, acceleration_disabled: bool = False) -> None:
        super().__init__()
        self.system_prompt_builder: Any = None
        self._agent: Any | None = None
        self._acceleration_disabled = bool(acceleration_disabled)

    def init(self, agent: Any) -> None:
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        _builder = getattr(agent, "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder = None
        self._agent = None

    def _resolve_priority(self, name: str, default_priority: int) -> int:
        if self.system_prompt_builder is None:
            return default_priority
        existing = self.system_prompt_builder.get_section(name)
        return existing.priority if existing is not None else default_priority

    @staticmethod
    def _resolve_language() -> str:
        return "cn"

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        # Bridge path: ctx.agent is the inner ReActAgent. Refresh the prompt
        # builder from it, but keep self._agent as the DeepAgent from init().
        _builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder

        if self.system_prompt_builder is None:
            return

        if self._acceleration_disabled:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
            logger.debug(
                "[SkillTurboPromptRail] skip skill_turbo_guide: %s is disabled",
                _SKILL_ACCELERATION_TOOL,
            )
            return

        # 请求期刷新工具描述（外部发现依赖请求级上下文），并取同源清单。
        # 刷新先于指南注入，保证两处清单一致。
        try:
            skill_names = _refresh_description_and_list_skills()
        except Exception as exc:
            logger.warning(
                "[SkillTurboPromptRail] refresh description failed: %s", exc,
            )
            skill_names = []

        if not skill_names:
            # 当前请求上下文无可见加速技能：整段不注入（对齐 acceleration_disabled
            # 先例——加速通道不可用即不引导，避免强制指令驱使 LLM 空跑降级往返）。
            self.system_prompt_builder.remove_section(_SECTION_NAME)
            logger.debug(
                "[SkillTurboPromptRail] skip skill_turbo_guide: no supported skills visible",
            )
            return

        language = self._resolve_language()
        try:
            text = _build_skill_turbo_guide_text(language, skill_names)
            self.system_prompt_builder.add_section(PromptSection(
                name=_SECTION_NAME,
                content={language: text},
                priority=self._resolve_priority(
                    _SECTION_NAME, PromptPriority.SKILL_PROTOCOL,
                ),
            ))
        except Exception as exc:
            logger.warning(
                "[SkillTurboPromptRail] build skill_turbo_guide section failed: %s", exc,
            )


__all__ = ["SkillTurboPromptRail"]
