# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject the dynamic Tool Usage Rules section without workspace context files."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
import openjiuwen.harness.prompts.sections.context as context_sections
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import (
    ContextAssembleRail,
)

_TOOL_USAGE_SECTION_PRIORITY = 14


class ToolUsagePromptRail(DeepAgentRail):
    """Render rules for the tools that are actually registered on this agent.

    Code and Design intentionally do not register ``ContextAssembleRail``:
    that rail also injects workspace context files.  This narrow rail reuses
    its tool-section builder while keeping those mode prompts free of unrelated
    workspace content.  The product patch gives the section priority 14,
    immediately after the shared Safety section (13), regardless of rail
    execution order.
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
        # Resolve from the module at call time so the shared prompt override's
        # placement patch also applies to Code and Design.
        section = context_sections.build_tools_section(self._ability_manager, language)
        if section is None:
            self.system_prompt_builder.remove_section("tools")
            return
        # This rail is the last writer for Code/Design.  Set the final object
        # priority here rather than relying on an earlier builder monkey patch.
        section.priority = _TOOL_USAGE_SECTION_PRIORITY
        self.system_prompt_builder.add_section(section)


class OrderedContextAssembleRail(ContextAssembleRail):
    """Product-scoped Context rail with deterministic Tool Usage placement.

    ``ContextAssembleRail`` imports its tool-section builder at module load,
    before the product's tool-text override can replace that helper. Rebuild
    the final Tools section here after the upstream rail runs. Keeping this
    behavior on the product-owned subclass avoids changing unrelated agents
    that happen to share the Python process.
    """

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await super().before_model_call(ctx)
        if self.system_prompt_builder is None or self.workspace is None:
            return
        language = getattr(self.system_prompt_builder, "language", "en") or "en"
        section = context_sections.build_tools_section(self._ability_manager, language)
        if section is None:
            self.system_prompt_builder.remove_section("tools")
            return
        section.priority = _TOOL_USAGE_SECTION_PRIORITY
        self.system_prompt_builder.add_section(section)
