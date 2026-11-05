# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Step-back prompt rail.

Tracks consecutive shell-command failures across turns.  When the agent has
failed the same way ``step_back_after`` times in a row without a successful
shell call in between, a high-priority system-prompt section is injected
instructing it to stop making incremental tweaks and instead rethink its
approach from scratch.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

logger = logging.getLogger(__name__)

_SECTION_NAME = "step_back_prompt"
_PRIORITY = 94
_SESSION_KEY = "_step_back_consecutive_failures"

# Tool names that execute shell / OS commands.
_SHELL_TOOLS: frozenset[str] = frozenset(
    {"mcp_exec_command", "bash", "run_bash", "execute_command", "run_command", "run_shell"}
)

# JSON keys that carry the process exit code.
_EXIT_KEYS: tuple[str, ...] = ("exit_code", "exitCode", "returncode", "return_code")


def _parse_exit_code(result: str) -> int | None:
    """Return the exit code from a JSON tool result, or None if not parseable."""
    if not result:
        return None
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return None
        for key in _EXIT_KEYS:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool):
                # JSON booleans are not exit codes; treating ``false`` as 0
                # would reset the failure counter on a tool that never ran.
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    except (ValueError, TypeError):
        pass
    return None


def _build_section(consecutive: int) -> str:
    return (
        f"**You have encountered {consecutive} consecutive shell failures without a "
        f"single success in between.**\n\n"
        "Do NOT make another incremental fix — that approach is not working.\n\n"
        "**Step back completely and follow this process before writing any more code:**\n\n"
        "1. Re-read the task requirements from the beginning.\n"
        "2. Identify what is *fundamentally* wrong with your current approach "
        "(not just the surface-level error message).\n"
        "3. Design a completely different strategy.\n"
        "4. Execute the new strategy end-to-end.\n\n"
        "Minor tweaks and retries of the same approach will continue to fail. "
        "A fresh perspective is required."
    )


class StepBackRail(DeepAgentRail):
    """Inject a rethink directive after repeated consecutive shell failures.

    The rail watches every call to a shell execution tool
    (``mcp_exec_command`` and common aliases).  A non-zero exit code counts
    as a failure; exit code 0 resets the counter.  Once the consecutive
    failure count reaches ``step_back_after``, a ``PromptSection`` (priority
    94) replaces any previous section with an escalating message that names
    the exact failure count and instructs the agent to step back.

    The counter is stored in session state under ``_step_back_consecutive_failures``
    so it persists across context compression events.

    Priority 10 — early in the rail chain so the directive is visible when
    later rails add their own sections.
    """

    priority = 10

    def __init__(self, step_back_after: int = 3) -> None:
        super().__init__()
        self._step_back_after = max(1, int(step_back_after))
        self.system_prompt_builder = None

    # ------------------------------------------------------------------
    # Rail lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: Any) -> None:  # noqa: ANN401
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:  # noqa: ANN401
        if self.system_prompt_builder:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder = None

    # ------------------------------------------------------------------
    # Track shell-call outcomes
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        tool_name: str = ctx.inputs.tool_name or ""
        if tool_name not in _SHELL_TOOLS:
            return

        exit_code = _parse_exit_code(ctx.inputs.tool_result or "")
        if exit_code is None:
            return  # can't determine outcome — leave counter unchanged

        consecutive = ctx.session.get_state(_SESSION_KEY) or 0
        if exit_code == 0:
            if consecutive > 0:
                logger.debug("[StepBack] Shell success — resetting failure counter (was %d)", consecutive)
            consecutive = 0
        else:
            consecutive += 1
            logger.debug("[StepBack] Shell failure — consecutive count now %d", consecutive)

        ctx.session.update_state({_SESSION_KEY: consecutive})

    async def on_tool_exception(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Count tool exceptions on shell tools the same as non-zero exit codes."""
        tool_name: str = ctx.inputs.tool_name or ""
        if tool_name not in _SHELL_TOOLS:
            return
        consecutive = (ctx.session.get_state(_SESSION_KEY) or 0) + 1
        logger.debug("[StepBack] Shell exception — consecutive count now %d", consecutive)
        ctx.session.update_state({_SESSION_KEY: consecutive})

    # ------------------------------------------------------------------
    # Inject / withdraw section before each LLM call
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        if not self.system_prompt_builder:
            return

        consecutive: int = ctx.session.get_state(_SESSION_KEY) or 0
        self.system_prompt_builder.remove_section(_SECTION_NAME)

        if consecutive >= self._step_back_after:
            body = _build_section(consecutive)
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": body, "en": body},
                    priority=_PRIORITY,
                )
            )
