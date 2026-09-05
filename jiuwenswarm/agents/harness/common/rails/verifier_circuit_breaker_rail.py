# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verifier-aware circuit breaker rail.

Tracks the verifier failure signature across turns.  When the identical
test failure appears ``break_after`` times in a row without any change in
the error, a high-priority system-prompt section is injected instructing the
agent to completely abandon its current approach and start from scratch.

This addresses the most destructive SkillsBench failure pattern:
  agent runs verifier → same test fails → marginal fix → repeat ×15 → timeout.

The rail is intentionally narrow: it only fires on verifier-style output
(pytest-formatted test results) so it does not interfere with ordinary
shell failures that are already handled by StepBackRail.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

logger = logging.getLogger(__name__)

_SECTION_NAME = "verifier_circuit_breaker"
_PRIORITY = 96          # Above StepBackRail (94) — circuit breaker takes precedence

_SESSION_KEY_SIG = "_vcb_failure_sig"
_SESSION_KEY_CONSECUTIVE = "_vcb_consecutive"

# Shell tool names that can invoke the verifier.
_SHELL_TOOLS: frozenset[str] = frozenset(
    {"mcp_exec_command", "bash", "run_bash", "execute_command", "run_command", "run_shell"}
)

# At least 2 of these must appear to classify output as verifier output.
# Matching is case-insensitive (see _is_verifier_output), so only lowercase
# forms are kept — an uppercase duplicate would let a single "passed" or
# "failed" hit two markers and satisfy the threshold.
_VERIFIER_MARKERS: tuple[str, ...] = (
    "test session starts",
    "test_outputs.py",
    "pytest",
    "verifier",
    "reward.txt",
    "AssertionError",
    "passed",
    "failed",
)

# Reward-file success values: exactly 1 (or 1.0 / 1.00), never 10 / 100 / 1.5.
_REWARD_ONE_RE = re.compile(r"reward\s*:\s*1(?:\.0+)?(?![\d.])", re.IGNORECASE)
_REWARD_JSON_ONE_RE = re.compile(r'"reward"\s*:\s*1(?:\.0+)?(?![\d.])', re.IGNORECASE)

# Regex patterns that extract the meaningful failure lines.
_FAILURE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^FAILED\s+\S+.*$", re.MULTILINE),
    re.compile(r"AssertionError[^\n]*", re.MULTILINE),
    re.compile(r"^E\s+Assert[^\n]*$", re.MULTILINE),
    re.compile(r"^E\s+[^\n]{10,}$", re.MULTILINE),
)

# Patterns whose variable parts are stripped before hashing.
_NORMALIZE: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"/tmp/[\w/.-]+"), "/tmp/..."),
    (re.compile(r"\d+\.\d+s\b"), "X.Xs"),
    (re.compile(r"\bline \d+\b"), "line N"),
    (re.compile(r"\bin \d+ms\b"), "in Nms"),
    (re.compile(r"\bat 0x[0-9a-f]+\b"), "at 0x..."),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(raw: str) -> str:
    """Unwrap JSON-wrapped tool result and return the displayable text."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            parts = [
                str(data[k])
                for k in ("stdout", "stderr", "output", "text", "content")
                if data.get(k)
            ]
            if parts:
                return "\n".join(parts)
    except (ValueError, TypeError):
        pass
    return raw


def _is_verifier_output(text: str) -> bool:
    """Return True when *text* looks like output from a test/verifier run."""
    lower = text.lower()
    hits = sum(1 for m in _VERIFIER_MARKERS if m.lower() in lower)
    return hits >= 2


def _is_verifier_success(text: str) -> bool:
    """Return True when the verifier reports all tests passing."""
    # pytest: "N passed" with no "failed" anywhere
    if re.search(r"\d+ passed", text) and "failed" not in text.lower():
        return True
    # reward file embedded in output — only exact 1 (or 1.0/1.00) counts,
    # not 10 / 100 / 1.5
    if _REWARD_ONE_RE.search(text) or _REWARD_JSON_ONE_RE.search(text):
        return True
    return False


def _failure_signature(text: str) -> str:
    """Return a stable 16-hex fingerprint of the failure content."""
    lines: list[str] = []
    for pattern in _FAILURE_PATTERNS:
        lines.extend(pattern.findall(text))

    raw = "\n".join(lines[:20]) if lines else text[-600:].strip()

    normalized = raw
    for pattern, replacement in _NORMALIZE:
        normalized = pattern.sub(replacement, normalized)

    return hashlib.md5(normalized.encode()).hexdigest()[:16]  # noqa: S324


def _build_section(consecutive: int, break_after: int) -> str:
    escalated = consecutive >= break_after * 2

    if escalated:
        return (
            f"**CIRCUIT BREAKER — the same verifier failure has appeared "
            f"{consecutive} times in a row.**\n\n"
            "Your current fix strategy is **fundamentally broken**. Abandon it entirely.\n\n"
            "**Required steps before writing any more code:**\n"
            "1. Re-read the task description and the failing assertion from scratch.\n"
            "2. Identify the *root cause* — not the surface-level error you have been patching.\n"
            "3. Undo your recent changes and return to a clean baseline.\n"
            "4. Design and implement a completely different solution.\n\n"
            "Every additional marginal fix will produce the same failure. A full reset is required."
        )

    return (
        f"**CIRCUIT BREAKER — the same verifier failure has appeared "
        f"{consecutive} times in a row.**\n\n"
        "Incremental fixes are not working. **Stop patching and rethink your approach.**\n\n"
        "Before making any further code changes:\n"
        "1. Re-read the failing assertion — what *exactly* is it checking?\n"
        "2. Is the output format correct? Is the output file path correct?\n"
        "3. Are you solving the right sub-problem, or treating a symptom?\n"
        "4. Design a different strategy — not a variation of what you have been doing.\n\n"
        "Do not make another marginal change to the same code you have been editing."
    )


# ---------------------------------------------------------------------------
# Rail
# ---------------------------------------------------------------------------

class VerifierCircuitBreakerRail(DeepAgentRail):
    """Inject a rethink directive when identical verifier failures repeat.

    After each shell tool call the rail inspects the output.  If it
    looks like pytest/verifier output, the failure signature (a hash of
    the failing assertion lines) is stored in session state.  When the
    same signature appears ``break_after`` times consecutively a
    high-priority ``PromptSection`` (priority 96) is injected before the
    next LLM call.  The section escalates at ``break_after * 2``
    repetitions to an even stronger "abandon everything" directive.

    A successful verifier run (all tests pass) clears the counter so the
    rail does not interfere after the agent has corrected its approach.

    State is stored in session state (survives context compression):

    * ``_vcb_failure_sig``    — MD5 fingerprint of the current failure
    * ``_vcb_consecutive``    — how many times this sig has appeared in a row

    Priority 11 — runs just after StepBackRail (10); the PromptSection
    uses priority 96 so it appears above StepBackRail's section (94)
    when both are active.
    """

    priority = 11

    def __init__(self, break_after: int = 3) -> None:
        super().__init__()
        self._break_after = break_after
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
    # Track verifier outcomes after each shell call
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        tool_name: str = ctx.inputs.tool_name or ""
        if tool_name not in _SHELL_TOOLS:
            return

        text = _extract_text(ctx.inputs.tool_result or "")
        if not _is_verifier_output(text):
            return

        if _is_verifier_success(text):
            logger.debug("[VCB] Verifier passed — clearing circuit breaker state")
            ctx.session.update_state({
                _SESSION_KEY_SIG: None,
                _SESSION_KEY_CONSECUTIVE: 0,
            })
            return

        sig = _failure_signature(text)
        current_sig: str | None = ctx.session.get_state(_SESSION_KEY_SIG)
        consecutive: int = ctx.session.get_state(_SESSION_KEY_CONSECUTIVE) or 0

        if sig == current_sig:
            consecutive += 1
        else:
            # Different failure — new problem, reset counter
            consecutive = 1

        logger.debug(
            "[VCB] Verifier failure recorded: sig=%s consecutive=%d", sig[:8], consecutive
        )
        ctx.session.update_state({
            _SESSION_KEY_SIG: sig,
            _SESSION_KEY_CONSECUTIVE: consecutive,
        })

    # ------------------------------------------------------------------
    # Inject / withdraw section before each LLM call
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        if not self.system_prompt_builder:
            return

        consecutive: int = ctx.session.get_state(_SESSION_KEY_CONSECUTIVE) or 0
        self.system_prompt_builder.remove_section(_SECTION_NAME)

        if consecutive >= self._break_after:
            body = _build_section(consecutive, self._break_after)
            self.system_prompt_builder.add_section(
                PromptSection(
                    name=_SECTION_NAME,
                    content={"cn": body, "en": body},
                    priority=_PRIORITY,
                )
            )
            logger.debug("[VCB] Circuit breaker active (consecutive=%d)", consecutive)
