# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load-aware subagent prompt extension for browser delegation."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.subagent import SubagentRail

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)


class BrowserTaskPromptRail(SubagentRail):
    """Append browser policy when the browser subagent is available."""

    def __init__(
        self,
        *,
        enable_async_subagent: bool = False,
        enable_subagent_runtime: bool = False,
    ) -> None:
        super().__init__(
            enable_async_subagent=enable_async_subagent,
            enable_subagent_runtime=enable_subagent_runtime,
            task_prompt_extension=self._task_prompt_extension,
            synchronous_subagent_types={"browser_agent"},
        )

    def _task_prompt_extension(
        self,
        ctx: AgentCallbackContext,
        language: str,
    ) -> str | None:
        if not self._has_browser_agent(ctx.agent):
            return None
        return build_browser_task_prompt(language)

    def _has_browser_agent(self, agent: object) -> bool:
        deep_config = getattr(agent, "deep_config", None)
        subagents = getattr(deep_config, "subagents", None) or []
        return any(
            self._extract_agent_meta(spec)[0] == "browser_agent"
            for spec in subagents
        )


__all__ = ["BrowserTaskPromptRail"]
