# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""No-progress guard rail for the outer task loop.

Tracks consecutive outer-loop iterations that produce no-tool "answer" results
with minimal content — the hung-agent loop pattern that burned 15k seconds in
Category E SkillsBench tasks. On the penultimate iteration, injects a steering
nudge encouraging the model to call a tool. On the final iteration, aborts the
outer loop via LoopCoordinator.request_abort().

This rail is benchmark-gated: enable via `react.task_loop_no_progress_guard.enabled: true`
in the bench overlay config. Default is disabled to avoid affecting interactive use.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

logger = logging.getLogger(__name__)

_SESSION_KEY_CONSECUTIVE = "_npg_consecutive_empty"
_SESSION_KEY_TOOLS_THIS_ROUND = "_npg_tools_this_round"


@dataclass
class NoProgressGuardConfig:
    """Configuration for the no-progress guard rail."""

    enabled: bool = False
    max_consecutive_empty_answers: int = 3
    min_answer_chars: int = 20
    steering_nudge_threshold: int = 2  # Nudge on the (max-1)th empty iteration


class NoProgressGuardRail(DeepAgentRail):
    """Rail that detects and stops hung no-tool-call loops in the outer task loop.

    Pattern detected:
      - Outer iteration produces result_type="answer" (no-tool inner completion)
      - Output is shorter than `min_answer_chars` chars
      - No tools were called during the iteration (precise, not approximate)

    On threshold-1: inject steering to encourage tool use.
    On threshold: call loop_coordinator.request_abort() to terminate the loop.

    Session state keys:
      - _npg_consecutive_empty: count of consecutive empty rounds
      - _npg_tools_this_round: tool count for the current round (reset each iteration)
    """

    def __init__(self, config: NoProgressGuardConfig, language: str = "en"):
        super().__init__()
        self.config = config
        self.language = language
        logger.info(
            "[NPG] NoProgressGuardRail initialized: enabled=%s, max=%d, min_chars=%d",
            self.config.enabled,
            self.config.max_consecutive_empty_answers,
            self.config.min_answer_chars,
        )

    async def before_task_iteration(
        self,
        ctx: AgentCallbackContext,  # noqa: ARG002
    ) -> None:
        """Reset the per-round tool counter at the start of each outer iteration."""
        if not self.config.enabled:
            return

        ctx.session.update_state({_SESSION_KEY_TOOLS_THIS_ROUND: 0})
        logger.debug("[NPG] Reset per-round tool counter")

    async def after_tool_call(
        self, ctx: AgentCallbackContext, **kwargs: Any  # noqa: ARG002
    ) -> None:
        """Increment the per-round tool counter on each tool execution."""
        if not self.config.enabled:
            return

        tools_this_round = ctx.session.get_state(_SESSION_KEY_TOOLS_THIS_ROUND) or 0
        tools_this_round += 1
        ctx.session.update_state({_SESSION_KEY_TOOLS_THIS_ROUND: tools_this_round})
        logger.debug("[NPG] Tool count this round: %d", tools_this_round)

    async def after_task_iteration(
        self, ctx: AgentCallbackContext, **kwargs: Any  # noqa: ARG002
    ) -> None:
        """Evaluate the iteration result and track/act on consecutive empty rounds.

        An iteration is considered "empty" if:
          - result_type == "answer" (set only at the no-tool-call break in react_agent)
          - tools_this_round == 0 (precise, not inferred)
          - len(str(output)) < min_answer_chars

        On (max-1) consecutive empties: push steering nudge ("You have not called any
        tools. Immediately invoke a tool to make progress.").

        On max consecutive empties: call loop_coordinator.request_abort() to stop
        the outer loop immediately.
        """
        if not self.config.enabled:
            return

        result = ctx.inputs.result or {}
        result_type = result.get("result_type", "")
        output = result.get("output", "")

        # Only count iterations that ended with a no-tool "answer"
        if result_type != "answer":
            consecutive_empty = 0
            ctx.session.update_state({_SESSION_KEY_CONSECUTIVE: consecutive_empty})
            logger.debug("[NPG] Reset counter (non-answer result_type=%s)", result_type)
            return

        tools_this_round = ctx.session.get_state(_SESSION_KEY_TOOLS_THIS_ROUND) or 0
        if tools_this_round > 0:
            # Round made tool calls, even if it ended with an answer
            consecutive_empty = 0
            ctx.session.update_state({_SESSION_KEY_CONSECUTIVE: consecutive_empty})
            logger.debug("[NPG] Reset counter (tools_this_round=%d)", tools_this_round)
            return

        output_len = len(str(output or ""))
        if output_len >= self.config.min_answer_chars:
            # Output is substantive enough; not a hung loop
            consecutive_empty = 0
            ctx.session.update_state({_SESSION_KEY_CONSECUTIVE: consecutive_empty})
            logger.debug("[NPG] Reset counter (output_len=%d)", output_len)
            return

        # Round is empty: increment counter
        consecutive_empty = ctx.session.get_state(_SESSION_KEY_CONSECUTIVE) or 0
        consecutive_empty += 1
        ctx.session.update_state({_SESSION_KEY_CONSECUTIVE: consecutive_empty})
        logger.warning(
            "[NPG] Empty iteration detected: iteration=%d, consecutive=%d/%d, "
            "output_len=%d, tools=%d",
            ctx.inputs.iteration,
            consecutive_empty,
            self.config.max_consecutive_empty_answers,
            output_len,
            tools_this_round,
        )

        max_allowed = self.config.max_consecutive_empty_answers
        steering_threshold = max_allowed - 1

        if consecutive_empty >= steering_threshold and consecutive_empty < max_allowed:
            # Penultimate iteration: push steering nudge
            nudge_msg = (
                "You have not called any tools in the last few iterations. "
                "To make progress, immediately invoke a relevant tool. "
                "Do not provide text-only responses without tool execution."
            )
            ctx.push_steering(nudge_msg)
            logger.info(
                "[NPG] Steering nudged (consecutive=%d/%d)", consecutive_empty, max_allowed
            )

        if consecutive_empty >= max_allowed:
            # Final threshold: abort the outer loop
            logger.critical(
                "[NPG] Max consecutive empty answers (%d) reached. "
                "Aborting outer task loop to prevent hung state.",
                max_allowed,
            )
            coordinator = getattr(ctx.agent, "loop_coordinator", None)
            if coordinator:
                coordinator.request_abort()
            else:
                logger.error("[NPG] LoopCoordinator not available on agent; cannot abort")