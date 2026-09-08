# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Channel-aware ``task_tool`` prompt extension for browser delegation."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.subagent import SubagentRail

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)


class BrowserTaskPromptRail(SubagentRail):
    """Append browser policy to ``task_tool`` only for the Web channel."""

    def __init__(
        self,
        channel: str = "web",
        include_usage_rules: bool = True,
    ) -> None:
        self._channel = self._normalize_channel(channel)
        self._include_usage_rules = include_usage_rules
        super().__init__(task_prompt_extension=self._task_prompt_extension)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Keep task_tool callable while allowing Office to omit its prompt block."""
        await super().before_model_call(ctx)
        if not self._include_usage_rules and self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(SectionName.TASK_TOOL)

    def set_channel(self, channel: str) -> None:
        """Update the channel for the current request."""

        self._channel = self._normalize_channel(channel)

    def _task_prompt_extension(
        self,
        ctx: AgentCallbackContext,
        language: str,
    ) -> str | None:
        del ctx
        if self._channel != "web":
            return None
        return build_browser_task_prompt(language)

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return str(channel or "").strip().lower() or "web"


__all__ = ["BrowserTaskPromptRail"]
