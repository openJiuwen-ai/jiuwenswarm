# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Iteration-budget awareness rail.

Injects a system-prompt warning section when the agent is running low on
iterations so it can prioritise finishing current work instead of starting
new long subtasks.
"""
from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

_SECTION_NAME = "iteration_budget_warning"
_WARNING_PRIORITY = 96  # just after runtime.model_answer_policy (95)


class IterationBudgetRail(DeepAgentRail):
    """Warn the agent when remaining iterations fall below a threshold.

    Priority 6 keeps this just after RuntimePromptRail (5) so it always
    fires in each model-call cycle.
    """

    priority = 6

    def __init__(self, max_iterations: int, warning_threshold: int) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._max_iterations = max_iterations
        self._warning_threshold = warning_threshold

    def init(self, agent: Any) -> None:
        """Acquire system_prompt_builder from agent."""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        """Remove injected section and release reference."""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder = None

    async def before_invoke(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Clear any stale warning at the start of a new invocation."""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Inject or remove the budget warning before every LLM call."""
        if not self.system_prompt_builder:
            return

        iteration = ctx.session.get_state("iteration")
        if iteration is None:
            return

        remaining = self._max_iterations - int(iteration)
        self.system_prompt_builder.remove_section(_SECTION_NAME)

        if remaining <= self._warning_threshold:
            warning_text = (
                f"**Iteration budget notice**: you have used {int(iteration)} of "
                f"{self._max_iterations} iterations and have approximately "
                f"{remaining} iteration(s) remaining.\n"
                "Prioritise completing or cleanly summarising your current task. "
                "Do not start new long subtasks. If you cannot finish, produce the "
                "best partial result you can and clearly state what remains."
            )
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": warning_text, "en": warning_text},
                    priority=_WARNING_PRIORITY,
                )
            )


__all__ = ["IterationBudgetRail"]
