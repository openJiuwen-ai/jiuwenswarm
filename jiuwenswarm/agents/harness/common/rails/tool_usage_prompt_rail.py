# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject the dynamic Tool Usage Rules section without workspace context files."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.sections.context import build_tools_section
from openjiuwen.harness.rails.base import DeepAgentRail


class ToolUsagePromptRail(DeepAgentRail):
    """Render rules for the tools that are actually registered on this agent.

    Code and Design intentionally do not register ``ContextAssembleRail``:
    that rail also injects workspace context files.  This narrow rail reuses
    its tool-section builder while keeping those mode prompts free of unrelated
    workspace content.  The section's upstream priority is 30, after Safety
    (12), regardless of rail execution order.
    """

    priority = 6

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._ability_manager = None

    def init(self, agent) -> None:  # type: ignore[no-untyped-def]
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._ability_manager = getattr(agent, "ability_manager", None)

    def uninit(self, agent) -> None:  # type: ignore[no-untyped-def]
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("tools")
        self.system_prompt_builder = None
        self._ability_manager = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return
        language = getattr(self.system_prompt_builder, "language", "en") or "en"
        section = build_tools_section(self._ability_manager, language)
        if section is None:
            self.system_prompt_builder.remove_section("tools")
            return
        self.system_prompt_builder.add_section(section)
