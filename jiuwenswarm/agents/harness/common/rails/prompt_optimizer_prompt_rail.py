# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt rail for the RLAF-P prompt optimizer.

Injects guidance on when to reach for ``optimize_prompt`` — but only when that tool
is actually exposed and ``symphony.optimization.enabled`` is true. Mirrors
:class:`jiuwenswarm.agents.harness.common.rails.symphony_orchestration_prompt_rail.SymphonyOrchestrationPromptRail`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

LOGGER = logging.getLogger(__name__)

_ConfigBaseProvider = dict[str, Any] | Callable[[], dict[str, Any] | None] | None


def _render_optimizer_prompt(config_base: dict[str, Any] | None = None) -> str:
    try:
        from jiuwenswarm.symphony.optimization.config import load_optimization_config

        config = (
            load_optimization_config()
            if config_base is None
            else load_optimization_config(config_base)
        )
        if not config.enabled:
            return ""
    except Exception as exc:
        LOGGER.warning(
            "PromptOptimizerPromptRail: config load failed, guidance suppressed: %s", exc
        )
        return ""
    return """
## Prompt Optimization

When the user asks you to tune, improve, or optimize a SYSTEM PROMPT for a task — or
when a task will be run repeatedly and a stronger system prompt would clearly help —
call `optimize_prompt` with the task `objective` (and a few input/expected `cases`
when you have them) instead of hand-tweaking the prompt yourself. The tool runs an
RL-style feedback loop that generates candidate prompts, executes and scores them, and
returns the best one along with its reward. Present the returned `best_prompt` to the
user and briefly note the reward gain. For ordinary one-off requests that do not need a
reusable prompt, continue normally without it.
"""


class PromptOptimizerPromptRail(DeepAgentRail):
    """Inject prompt-optimizer guidance only when the tool is available."""

    priority = 97
    SECTION_NAME = "prompt_optimization"
    SECTION_PRIORITY = 41
    OPTIMIZE_TOOL_NAME = "optimize_prompt"

    def __init__(self, *, config_base: _ConfigBaseProvider = None) -> None:
        super().__init__()
        self._config_base = config_base
        self.system_prompt_builder = None

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return

        if not self._has_optimize_tool(ctx):
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            return

        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        content = _render_optimizer_prompt(self._resolve_config_base())
        if not content.strip():
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            return

        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={language: content},
                priority=self.SECTION_PRIORITY,
            )
        )

    @classmethod
    def _has_optimize_tool(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return False
        return any(cls._model_tool_name(tool) == cls.OPTIMIZE_TOOL_NAME for tool in tools)

    @staticmethod
    def _model_tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict):
                return str(function.get("name", "") or "")
            return str(tool.get("name", "") or "")
        function = getattr(tool, "function", None)
        if isinstance(function, dict):
            return str(function.get("name", "") or "")
        function_name = getattr(function, "name", None)
        if function_name is not None:
            return str(function_name)
        name = getattr(tool, "name", None)
        if name is not None:
            return str(name)
        card = getattr(tool, "card", None)
        return str(getattr(card, "name", "") or "")

    def _resolve_config_base(self) -> dict[str, Any] | None:
        if callable(self._config_base):
            return self._config_base()
        return self._config_base


__all__ = ["PromptOptimizerPromptRail"]
