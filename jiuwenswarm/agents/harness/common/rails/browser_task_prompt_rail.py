# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load-aware subagent prompt extension for browser / desktop delegation."""

from __future__ import annotations

import os

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.subagent import SubagentRail

from jiuwenswarm.agents.harness.common.cua_defaults import cua_model_rejects_images
from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)
from jiuwenswarm.agents.harness.common.prompt.cua_task_prompt import (
    build_cua_task_prompt,
    build_cua_unavailable_prompt,
)
from jiuwenswarm.common.cua_config import is_cua_backend_available


try:
    from openjiuwen.harness.tools import is_free_search_enabled
except ImportError:  # Older SDKs may not export the availability helper.

    def is_free_search_enabled() -> bool:
        for name in ("FREE_SEARCH_DDG_ENABLED", "FREE_SEARCH_BING_ENABLED"):
            value = os.environ.get(name, "").strip().lower()
            if value in {"1", "true", "yes", "on", "enabled"}:
                return True
        return False


try:
    from openjiuwen.harness.tools import is_paid_search_enabled
except ImportError:

    def is_paid_search_enabled() -> bool:
        for name in ("BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY"):
            if os.environ.get(name, "").strip():
                return True
        return False


class BrowserTaskPromptRail(SubagentRail):
    """Append browser / desktop routing policy for the mounted subagents."""

    def __init__(
        self,
        *,
        enable_async_subagent: bool = False,
        enable_subagent_runtime: bool = False,
        cua_agent_enabled: bool = False,
    ) -> None:
        self._cua_agent_enabled = cua_agent_enabled
        self._cua_backend_available = cua_agent_enabled and is_cua_backend_available()
        self._channel: str | None = None
        self._deep_agent: object | None = None
        super().__init__(
            enable_async_subagent=enable_async_subagent,
            enable_subagent_runtime=enable_subagent_runtime,
            task_prompt_extension=self._task_prompt_extension,
            # Both stay on the synchronous task_tool even when the persistent
            # subagent runtime is on: agent-core's TaskTool owns their explicit
            # resume handling (browser_result / cua_result payloads).
            synchronous_subagent_types={"browser_agent", "cua_agent"},
        )

    def init(self, agent) -> None:
        # Model-call callbacks carry the inner ReActAgent as ``ctx.agent``; only
        # the DeepAgent handed to ``init`` exposes ``deep_config.subagents``.
        self._deep_agent = agent
        super().init(agent)

    def set_channel(self, channel: str | None) -> None:
        """Store the active runtime channel for this prompt rail."""
        value = str(channel or "").strip()
        self._channel = value or None

    def _task_prompt_extension(
        self,
        ctx: AgentCallbackContext,
        language: str,
    ) -> str | None:
        agent = self._deep_agent if self._deep_agent is not None else ctx.agent
        mounted = self._mounted_subagent_names(agent)
        has_browser = "browser_agent" in mounted
        # The adapter mounts cua_agent against the config default model; the
        # request model swapped in afterwards may still reject images.
        current_model = getattr(getattr(agent, "deep_config", None), "model", None)
        has_cua = "cua_agent" in mounted and not cua_model_rejects_images(current_model)
        sections: list[str] = []
        if has_browser:
            sections.append(
                build_browser_task_prompt(
                    language,
                    search_fallback_enabled=not is_paid_search_enabled() and not is_free_search_enabled(),
                )
            )
        if has_cua:
            sections.append(
                build_cua_task_prompt(language, browser_available=has_browser)
            )
        elif self._cua_agent_enabled:
            sections.append(
                build_cua_unavailable_prompt(language, backend_available=self._cua_backend_available)
            )
        if not sections:
            return None
        return "\n\n".join(sections)

    def _mounted_subagent_names(self, agent: object) -> set[str]:
        deep_config = getattr(agent, "deep_config", None)
        subagents = getattr(deep_config, "subagents", None) or []
        return {self._extract_agent_meta(spec)[0] for spec in subagents}


__all__ = ["BrowserTaskPromptRail"]
