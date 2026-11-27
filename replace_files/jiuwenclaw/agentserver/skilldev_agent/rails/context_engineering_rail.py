# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillDevContextEngineeringRail — SkillDev 专用 Context Engineering Rail。

相比 SDK 原版 ContextEngineeringRail，仅注入 tools 段（工具列表），
跳过 workspace 段（工作目录声明）和 context 段（读取 AGENT.md 等通用文件），
同时保留上下文压缩处理器和 offload 提示。

原因：
- workspace 段：SkillDev 的 system prompt 已在 prompts.py 中显式声明工作目录，无需重复注入。
- context 段：SkillDev 工作区不含 AGENT.md/SOUL.md 等通用配置文件，注入无意义且浪费 token。
- tools 段：工具列表对 Agent 正确调用工具仍然必要，予以保留。
"""
from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections.context import build_tools_section
from openjiuwen.harness.rails.context_engineering_rail import ContextEngineeringRail

OFFLOAD_HINT_CN = (
    "# 上下文压缩\n\n"
    "你的上下文在过长时会被自动压缩，"
    "并标记为[OFFLOAD: handle=<id>, type=<type>]。\n\n"
    "如果你认为需要读取隐藏的内容，"
    "可随时调用reload_original_context_messages工具。\n\n"
    "请勿猜测或编造缺失的内容。\n\n"
    '存储类型："in_memory"（会话缓存）'
)

OFFLOAD_HINT_EN = (
    "# Context Compression\n\n"
    "Your context will be automatically compressed when it becomes too long "
    "and marked with [OFFLOAD: handle=<id>, type=<type>].\n\n"
    'Call reload_original_context_messages(offload_handle="<id>", '
    'offload_type="<type>"), using the exact values from the marker.\n\n'
    "Do not guess or fabricate missing content.\n\n"
    'Storage types: "in_memory" (session cache)'
)


class SkillDevContextEngineeringRail(ContextEngineeringRail):
    """SkillDev 专用 CE Rail：仅注入 tools 段 + offload 提示，跳过 workspace/context 段。

    上下文压缩处理器（processors）通过父类 init 正常初始化，无需重复配置。
    """

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """仅注入工具列表和 offload 提示，跳过 workspace/context 段。"""
        if self.system_prompt_builder is None:
            return

        lang = self.system_prompt_builder.language or "cn"

        tools_section = build_tools_section(self._ability_manager, lang)
        if tools_section is not None:
            self.system_prompt_builder.add_section(tools_section)
        else:
            self.system_prompt_builder.remove_section("tools")

        self.system_prompt_builder.remove_section("workspace")
        self.system_prompt_builder.remove_section("context")

        hint = OFFLOAD_HINT_CN if lang == "cn" else OFFLOAD_HINT_EN
        self.system_prompt_builder.add_section(
            PromptSection(
                name="offload",
                content={lang: hint},
                priority=90,
            )
        )
