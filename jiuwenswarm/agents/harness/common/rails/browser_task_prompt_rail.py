# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load-aware subagent prompt extension for browser / desktop delegation."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.subagent import SubagentRail

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)
from jiuwenswarm.agents.harness.common.prompt.cua_task_prompt import (
    build_cua_task_prompt,
    build_cua_unavailable_prompt,
)


class BrowserTaskPromptRail(SubagentRail):
    """Append browser / desktop routing policy for the mounted subagents."""

    def __init__(
        self,
        *,
        enable_async_subagent: bool = False,
        enable_subagent_runtime: bool = False,
        cua_agent_enabled: bool = False,
    ) -> None:
        # Enabled in config but absent from the mounted subagents means the
        # adapter skipped it because the model rejects image input.
        self._cua_agent_enabled = cua_agent_enabled
        super().__init__(
            enable_async_subagent=enable_async_subagent,
            enable_subagent_runtime=enable_subagent_runtime,
            task_prompt_extension=self._task_prompt_extension,
            # Both stay on the synchronous task_tool even when the persistent
            # subagent runtime is on: agent-core's TaskTool owns their explicit
            # resume handling (browser_result / cua_result payloads).
            synchronous_subagent_types={"browser_agent", "cua_agent"},
        )

    def _task_prompt_extension(
        self,
        ctx: AgentCallbackContext,
        language: str,
    ) -> str | None:
        mounted = self._mounted_subagent_names(ctx.agent)
        has_browser = "browser_agent" in mounted
        has_cua = "cua_agent" in mounted
        sections: list[str] = []
        if has_browser:
            sections.append(build_browser_task_prompt(language))
        if has_cua:
            sections.append(
                build_cua_task_prompt(language, browser_available=has_browser)
            )
        elif self._cua_agent_enabled:
            sections.append(build_cua_unavailable_prompt(language))
        if not sections:
            return None
        return "\n\n".join(sections)

    def _mounted_subagent_names(self, agent: object) -> set[str]:
        deep_config = getattr(agent, "deep_config", None)
        subagents = getattr(deep_config, "subagents", None) or []
        return {self._extract_agent_meta(spec)[0] for spec in subagents}


__all__ = ["BrowserTaskPromptRail"]
