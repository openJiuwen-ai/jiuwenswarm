# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Channel-aware ``task_tool`` prompt extension for browser delegation."""

from __future__ import annotations

import re

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.subagent import SubagentRail
from openjiuwen.harness.subagent_types import listed_subagent_types

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)


_BROWSER_TASK_TOOL_DESCRIPTION_RULES = (
    re.compile(
        r'When subagent_type is "browser_agent".*?'
        r'Do not provide browser_capabilities for other subagent types\.',
        re.DOTALL,
    ),
    re.compile(
        r'当 subagent_type 为 "browser_agent" 时，还必须指定 browser_capabilities，'
        r'.*?其他子代理类型不要提供 browser_capabilities。',
        re.DOTALL,
    ),
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
        self._browser_agent_available = False
        super().__init__(task_prompt_extension=self._task_prompt_extension)

    def init(self, agent) -> None:
        """Register the subagent tool and hide browser-only metadata when needed."""

        self._browser_agent_available = self._has_browser_agent(agent)
        super().init(agent)
        self._sanitize_browser_metadata()

    def refresh_available_agents(self, agent) -> None:
        """Refresh the tool card without re-exposing unavailable browser options."""

        self._browser_agent_available = self._has_browser_agent(agent)
        super().refresh_available_agents(agent)
        self._sanitize_browser_metadata()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Keep task_tool callable while allowing Office to omit its prompt block."""
        await super().before_model_call(ctx)
        if not self._include_usage_rules and self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(SectionName.TASK_TOOL)

    def set_channel(self, channel: str) -> None:
        """Update the channel for the current request."""

        self._channel = self._normalize_channel(channel)

    @staticmethod
    def _has_browser_agent(agent) -> bool:
        config = getattr(agent, "deep_config", None)
        return "browser_agent" in listed_subagent_types(
            getattr(config, "subagents", None)
        )

    def _sanitize_browser_metadata(self) -> None:
        """Remove browser-only task-tool metadata when no browser agent exists."""

        if self._browser_agent_available:
            return
        for tool in self.tools or []:
            card = getattr(tool, "card", None)
            if getattr(card, "name", None) != "task_tool":
                continue

            description = getattr(card, "description", None)
            if isinstance(description, str):
                for rule in _BROWSER_TASK_TOOL_DESCRIPTION_RULES:
                    description = rule.sub("", description)
                card.description = description

            params = getattr(card, "input_params", None)
            properties = params.get("properties") if isinstance(params, dict) else None
            if isinstance(properties, dict):
                properties.pop("browser_capabilities", None)

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
