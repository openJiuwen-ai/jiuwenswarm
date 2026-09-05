# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Failure-pattern memory rail.

Records tool failures (both exceptions and error-containing results) in
session state and injects a running summary into the system prompt so the
agent knows which approaches have already failed and should not be retried.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

logger = logging.getLogger(__name__)

_SECTION_NAME = "failure_memory"
_WARNING_PRIORITY = 95
_SESSION_KEY = "_failure_memory"

# Substrings that indicate a tool result represents a failure rather than
# a successful (possibly empty) response.
_ERROR_PATTERNS: tuple[str, ...] = (
    "Traceback",
    "Error:",
    "Exception:",
    " error:",
    "FAILED",
    "returned non-zero exit status",
    "Permission denied",
    "No such file or directory",
    "command not found",
    "SyntaxError",
    "AssertionError",
    "FileNotFoundError",
    "ModuleNotFoundError",
    "ImportError",
    "exit code 1",
    "exit code 2",
    "exited with",
)


def _is_error_result(result: str) -> bool:
    """Return True if *result* text looks like a failure."""
    for pat in _ERROR_PATTERNS:
        if pat in result:
            return True
    return False


def _extract_snippet(result: str, max_chars: int = 300) -> str:
    """Return the first error-containing lines from *result*."""
    lines = result.splitlines()
    for i, line in enumerate(lines):
        for pat in _ERROR_PATTERNS:
            if pat in line:
                snippet = "\n".join(lines[i:i + 6])
                return snippet[:max_chars]
    return result[:max_chars]


def _summarize_args(args: Any, max_chars: int = 120) -> str:
    """Produce a short human-readable representation of tool arguments."""
    if not args:
        return ""
    try:
        s = json.dumps(args, default=str, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


class FailureMemoryRail(DeepAgentRail):
    """Accumulate tool failure patterns and surface them in the system prompt.

    Every time a tool call ends with an exception or an error-containing
    result, the failure is appended to a session-state list (key
    ``_failure_memory``).  Before each LLM call the rail rebuilds a
    ``PromptSection`` (priority 95) that lists all known failures, explicitly
    asking the agent to use a different approach rather than repeating them.

    This combats the common failure mode where an agent retries the exact same
    broken command, flag, or path dozens of times without learning that it
    doesn't work.

    Priority 7 — runs after RuntimePromptRail (5) but before most other
    dynamic rails, so the failure list is already present when any subsequent
    rail injects additional guidance.
    """

    priority = 7

    def __init__(self, max_failures: int = 10) -> None:
        super().__init__()
        self._max_failures = max_failures
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
    # Failure detection — after a normal (non-exception) tool call
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        raw = ctx.inputs.tool_result
        result: str = "" if raw is None else (raw if isinstance(raw, str) else str(raw))
        if not _is_error_result(result):
            return
        error_snippet = _extract_snippet(result)
        self._record(ctx, ctx.inputs.tool_name, ctx.inputs.tool_args, error_snippet)
        self._rebuild_section(ctx)

    # ------------------------------------------------------------------
    # Failure detection — when the tool raises an exception
    # ------------------------------------------------------------------

    async def on_tool_exception(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        exc_str = str(ctx.exception)[:300] if ctx.exception else "unknown exception"
        self._record(ctx, ctx.inputs.tool_name, ctx.inputs.tool_args, exc_str)
        self._rebuild_section(ctx)

    # ------------------------------------------------------------------
    # Inject section before every model call (covers the case where the
    # section was removed by context reset between turns)
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        failures = self._load(ctx)
        if failures:
            self._update_section(failures)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self, ctx: AgentCallbackContext) -> list[dict]:
        return ctx.session.get_state(_SESSION_KEY) or []

    def _record(
        self,
        ctx: AgentCallbackContext,
        tool_name: str,
        tool_args: Any,
        error: str,
    ) -> None:
        failures = self._load(ctx)
        entry: dict = {
            "tool": tool_name or "unknown",
            "args": _summarize_args(tool_args),
            "error": error,
        }
        # Skip exact duplicate of the most recent entry to avoid noise
        if failures and failures[-1]["error"] == error:
            logger.debug("[FailureMemory] Duplicate failure suppressed: %s", tool_name)
            return
        failures.append(entry)
        if len(failures) > self._max_failures:
            failures = failures[-self._max_failures:]
        ctx.session.update_state({_SESSION_KEY: failures})
        logger.debug(
            "[FailureMemory] Recorded failure #%d for tool '%s'", len(failures), tool_name
        )

    def _rebuild_section(self, ctx: AgentCallbackContext) -> None:
        failures = self._load(ctx)
        if failures:
            self._update_section(failures)

    def _update_section(self, failures: list[dict]) -> None:
        if not self.system_prompt_builder:
            return
        lines = [
            "**Previous failures in this session** — these exact approaches did not work.",
            "Do NOT repeat them. Use different arguments, a different tool, or a different strategy.\n",
        ]
        for i, f in enumerate(failures, 1):
            args_part = f"  args: {f['args']}" if f["args"] else ""
            lines.append(f"{i}. `{f['tool']}`{args_part}")
            # Indent the error so it reads as continuation
            for error_line in f["error"].splitlines():
                lines.append(f"   → {error_line}")
        body = "\n".join(lines)
        self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={"cn": body, "en": body},
                priority=_WARNING_PRIORITY,
            )
        )
