# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Review-queue rail for the RLAF-P prompt optimizer.

``PromptOptimizerPromptRail`` tells the leader how to *start* an optimization.
This rail closes the other gap: if an optimization happened without a human
watching (the leader decided on its own, or someone ran it out-of-band), there
was previously no way for anyone to find out a better prompt exists unless
they knew to go ask for it. This rail surfaces any unreviewed improvements —
:meth:`openjiuwen.dev_tools.tune.optimizer.prompt_search.memory.PromptMemory.pending` —
directly in the leader's own context, the same way
``PromptOptimizerPromptRail`` injects tool guidance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

LOGGER = logging.getLogger(__name__)

_ConfigBaseProvider = dict[str, Any] | Callable[[], dict[str, Any] | None] | None

_MAX_LISTED = 3


def _render_pending_section(config_base: dict[str, Any] | None = None) -> str:
    try:
        from jiuwenswarm.symphony.optimization.config import load_optimization_config
        from jiuwenswarm.symphony.optimization.factory import OptimizerRuntimeFactory

        config = (
            load_optimization_config()
            if config_base is None
            else load_optimization_config(config_base)
        )
        if not config.enabled or not config.memory_enabled:
            return ""
        memory = OptimizerRuntimeFactory(config).memory()
        pending = memory.pending(config.convergence_threshold)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("PromptOptimizerReviewRail: pending lookup failed: %s", exc)
        return ""

    if not pending:
        return ""

    lines = []
    for record in pending[:_MAX_LISTED]:
        objective = record.objective if len(record.objective) <= 90 else record.objective[:90] + "…"
        baseline = f"{record.baseline_reward:.2f}" if record.baseline_reward is not None else "none"
        lines.append(
            f'- record_id={record.record_id} · objective: "{objective}" · '
            f"reward {record.reward:.2f} (baseline {baseline}, gain +{record.gain:.2f})"
        )
    more = f"\n(+{len(pending) - _MAX_LISTED} more)" if len(pending) > _MAX_LISTED else ""

    return f"""
## Unreviewed Prompt Optimizations

The following previously optimized system prompts scored better than their prior
baseline but have not been confirmed as installed anywhere yet:
{chr(10).join(lines)}{more}

If any of these is relevant to the current task, mention it to the user (with the
reward gain) instead of assuming no better prompt exists. Once a human confirms it
has actually been put into use, call `mark_prompt_improvement_applied` with its
`record_id` so it stops being listed here. Do not install it yourself without
confirmation — the reward is a proxy signal, not a substitute for review.
"""


class PromptOptimizerReviewRail(DeepAgentRail):
    """Surface unreviewed prompt-optimization results to the team leader."""

    priority = 96
    SECTION_NAME = "prompt_optimization_review"
    SECTION_PRIORITY = 42
    REQUIRED_TOOL_NAME = "mark_prompt_improvement_applied"

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

        if not self._has_review_tool(ctx):
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            return

        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        content = await asyncio.to_thread(_render_pending_section, self._resolve_config_base())
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
    def _has_review_tool(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return False
        return any(cls._model_tool_name(tool) == cls.REQUIRED_TOOL_NAME for tool in tools)

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


__all__ = ["PromptOptimizerReviewRail"]
