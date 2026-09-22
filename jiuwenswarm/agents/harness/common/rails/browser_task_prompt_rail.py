# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load-aware subagent prompt extension for browser delegation."""

from __future__ import annotations

from inspect import signature
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.subagent import SubagentRail

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)


def _subagent_rail_init_kwargs(
    *,
    enable_async_subagent: bool,
    enable_subagent_runtime: bool,
    task_prompt_extension: Any,
    synchronous_subagent_types: set[str],
) -> dict[str, Any]:
    """Pass only kwargs the installed SDK SubagentRail actually accepts."""
    params = signature(SubagentRail.__init__).parameters
    kwargs: dict[str, Any] = {}
    if "enable_async_subagent" in params:
        kwargs["enable_async_subagent"] = enable_async_subagent
    if "enable_subagent_runtime" in params:
        kwargs["enable_subagent_runtime"] = enable_subagent_runtime
    if "task_prompt_extension" in params:
        kwargs["task_prompt_extension"] = task_prompt_extension
    if "synchronous_subagent_types" in params:
        kwargs["synchronous_subagent_types"] = synchronous_subagent_types
    return kwargs


class BrowserTaskPromptRail(SubagentRail):
    """Append browser policy when the browser subagent is available."""

    def __init__(
        self,
        *,
        enable_async_subagent: bool = False,
        enable_subagent_runtime: bool = False,
    ) -> None:
        super().__init__(
            **_subagent_rail_init_kwargs(
                enable_async_subagent=enable_async_subagent,
                enable_subagent_runtime=enable_subagent_runtime,
                task_prompt_extension=self._task_prompt_extension,
                synchronous_subagent_types={"browser_agent"},
            )
        )
        self.enable_subagent_runtime = bool(enable_subagent_runtime)

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
