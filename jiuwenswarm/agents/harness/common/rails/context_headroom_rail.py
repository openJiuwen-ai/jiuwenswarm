# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Context headroom rail.

Monitors the conversation context token usage before every model call and
injects escalating conciseness directives into the system prompt as the
context window fills up, slowing down the rate at which useful information
is lost to context compression.
"""
from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

logger = logging.getLogger(__name__)

_SECTION_NAME = "context_headroom"
_PRIORITY = 93          # dynamic zone, visible but after task/identity sections

_WARN_CONTENT = """\
**Context window filling up** — be concise from here on:
- Do not restate what you have already done or found.
- Prefer short confirmations over verbose explanations.
- Only include output that is essential for the next step.
"""

_CRITICAL_CONTENT = """\
**CRITICAL: Context window is nearly full** — compression is imminent.
- Be extremely brief in every response.
- Output only the minimum needed to continue the task.
- Do not repeat, summarise, or explain previous steps.
- If you have key findings that must survive compression, state them now in one short sentence.
"""


class ContextHeadroomRail(DeepAgentRail):
    """Inject conciseness guidance as the context window approaches capacity.

    Before each LLM call, the rail reads the current total token count from
    ``ctx.context.statistic()`` and compares it against the configured context
    window size.  Two thresholds control the response:

    * **warn_ratio** (default 0.60) — a moderate "be concise" nudge is
      injected.  At this level there is still headroom, but the agent should
      stop padding responses.
    * **critical_ratio** (default 0.80) — a strong directive is injected
      telling the agent to be extremely brief because automatic compression
      (which discards conversation history) is about to fire.

    When usage drops back below the lower threshold (e.g. after compression
    has run), the section is removed automatically.

    Priority 9 — runs very early so the conciseness directive is already
    present when all downstream rails add their sections.
    """

    priority = 9

    def __init__(
        self,
        warn_ratio: float = 0.60,
        critical_ratio: float = 0.80,
    ) -> None:
        super().__init__()
        if warn_ratio >= critical_ratio:
            logger.warning(
                "[ContextHeadroom] warn_ratio (%.2f) must be below "
                "critical_ratio (%.2f); swapping so the warn branch stays "
                "reachable",
                warn_ratio,
                critical_ratio,
            )
            warn_ratio, critical_ratio = critical_ratio, warn_ratio
        self._warn_ratio = warn_ratio
        self._critical_ratio = critical_ratio
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
    # Core hook
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        if not self.system_prompt_builder or ctx.context is None:
            return

        ratio = self._usage_ratio(ctx)
        if ratio is None:
            return

        self.system_prompt_builder.remove_section(_SECTION_NAME)

        if ratio >= self._critical_ratio:
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": _CRITICAL_CONTENT, "en": _CRITICAL_CONTENT},
                    priority=_PRIORITY,
                )
            )
            logger.debug(
                "[ContextHeadroom] Critical threshold reached (%.1f%% used)", ratio * 100
            )
        elif ratio >= self._warn_ratio:
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": _WARN_CONTENT, "en": _WARN_CONTENT},
                    priority=_PRIORITY,
                )
            )
            logger.debug(
                "[ContextHeadroom] Warn threshold reached (%.1f%% used)", ratio * 100
            )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _usage_ratio(self, ctx: AgentCallbackContext) -> float | None:
        """Return current token usage as a fraction of the context window, or None on error."""
        try:
            stats = ctx.context.statistic()
            current = stats.total_tokens
            max_tokens_raw = getattr(ctx.context, "_context_window_tokens", None)
            # Explicit is-None check: 0 is a real (invalid) value that must be
            # caught by the guard below, not silently replaced by the fallback.
            max_tokens: int = 100_000 if max_tokens_raw is None else int(max_tokens_raw)
            if max_tokens <= 0:
                return None
            return current / max_tokens
        except Exception as exc:
            logger.debug("[ContextHeadroom] Could not read context stats: %s", exc)
            return None
