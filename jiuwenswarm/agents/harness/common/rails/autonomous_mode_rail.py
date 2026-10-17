# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Autonomous execution mode rail.

When ``autonomy.enabled`` is set in config, injects a high-priority
system-prompt section (priority 9, just before INTRO at 10) that overrides
interactive hedging language with fully autonomous directives.

Useful in any environment where the agent operates without a human in the
loop — CI pipelines, benchmark evaluations, scripted agent runs, or any
automated workflow that requires end-to-end task completion without
supervision.
"""
from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

_SECTION_NAME = "autonomous_mode"

# Priority 9 — renders immediately before INTRO (10), so the LLM reads
# this framing before any other system-prompt content.
_SECTION_PRIORITY = 9

_AUTONOMOUS_DIRECTIVE = """\
# Autonomous Execution Mode

You are operating as a **fully autonomous agent** without a human supervisor. \
The following rules apply for this session and override any other instructions \
about asking for confirmation or clarification:

- **Never ask for clarification or confirmation.** The task description is \
complete and sufficient. Proceed directly based on what is given.
- **Never ask permission before acting.** All tool use is pre-authorised. \
Execute read, write, edit, and shell operations without prompting.
- **Never hedge or express uncertainty.** Make a decision and act on it. \
If you encounter ambiguity, choose the most reasonable interpretation and proceed.
- **Verify your own work.** Use available tools (tests, linters, build commands, \
verifier scripts) to confirm correctness. Iterate on failures autonomously.
- **Complete the task end-to-end.** Do not stop mid-task to report progress \
or ask if you should continue. Finish, verify, and then report the final outcome.
"""


class AutonomousModeRail(DeepAgentRail):
    """Inject autonomous-mode directives when running without a human in the loop.

    Priority 3 ensures this fires before RuntimePromptRail (5), so the
    section is already present when other rails inject their content.
    """

    priority = 3

    def __init__(self, enabled: bool) -> None:
        super().__init__()
        self._enabled = enabled
        self.system_prompt_builder = None

    def init(self, agent: Any) -> None:
        """Acquire system_prompt_builder and inject the section immediately."""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self._enabled and self.system_prompt_builder is not None:
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": _AUTONOMOUS_DIRECTIVE, "en": _AUTONOMOUS_DIRECTIVE},
                    priority=_SECTION_PRIORITY,
                )
            )

    def uninit(self, agent: Any) -> None:
        """Remove the injected section and release reference."""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder = None

    async def before_invoke(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Re-assert the section at the start of each invocation."""
        if not self._enabled or self.system_prompt_builder is None:
            return
        self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={"cn": _AUTONOMOUS_DIRECTIVE, "en": _AUTONOMOUS_DIRECTIVE},
                priority=_SECTION_PRIORITY,
            )
        )


__all__ = ["AutonomousModeRail"]
