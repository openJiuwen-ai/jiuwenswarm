# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tool-call deduplication rail."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

try:
    from openjiuwen.core.foundation.llm.schema.message import ToolMessage
except ImportError:
    ToolMessage = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_SECTION_NAME = "tool_dedup_warning"
_WARNING_PRIORITY = 97

# Tools whose results are deterministic; identical repeat calls are safe to short-circuit.
_CACHEABLE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_text_file",
        "read",
        "glob",
        "glob_file_search",
        "list_dir",
        "list_files",
        "grep",
        "search",
    }
)


def _cache_key(tool_name: str, tool_args: dict) -> str:
    args_hash = hashlib.md5(  # noqa: S324 — non-cryptographic, fast fingerprint only
        json.dumps(tool_args, sort_keys=True, default=str).encode()
    ).hexdigest()[:8]
    return f"{tool_name}:{args_hash}"


class ToolCallDeduplicationRail(DeepAgentRail):
    """Detect and suppress redundant tool calls within and across model turns.

    Two mechanisms are active when the rail is enabled:

    1. **Per-turn deduplication** — a cache is built from every tool execution
       within a single model response.  If the agent emits the same
       ``(tool_name, args)`` pair a second time in the same turn, the rail
       intercepts the call, returns the cached result immediately, and sets
       ``ctx.extra["_skip_tool"] = True`` so the real tool is never invoked.

    2. **Cross-turn repetition warning** — across the whole session the rail
       counts how many times each unique call has actually been executed.
       Once the count reaches ``warn_after``, it injects a system-prompt
       section (priority 97) reminding the agent that this tool+args
       combination has already been called repeatedly and the result is
       unlikely to change.

    Only tools listed in the cacheable whitelist are subject to deduplication;
    side-effecting tools (e.g. write_file, run_shell) are never intercepted.

    Priority 8 places this rail after RuntimePromptRail (5) and
    TaskDescriptionRail (4) but before any post-processing rails.
    """

    priority = 8

    def __init__(self, warn_after: int = 3) -> None:
        super().__init__()
        self._warn_after = warn_after
        self.system_prompt_builder = None
        # Per-turn cache: key → result string
        self._turn_cache: dict[str, str] = {}
        # Cross-turn execution count: key → int
        self._exec_counts: dict[str, int] = {}
        # Keys for which a warning section is currently active
        self._warned_keys: set[str] = set()

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
    # Per-turn cache reset
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Clear per-turn cache before each LLM call."""
        self._turn_cache = {}

    # ------------------------------------------------------------------
    # Tool interception
    # ------------------------------------------------------------------

    async def before_tool_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Check per-turn cache; skip real call if result is already known."""
        tool_name: str = ctx.inputs.tool_name or ""
        if tool_name not in _CACHEABLE_TOOLS:
            return

        tool_args: dict = ctx.inputs.tool_args or {}
        key = _cache_key(tool_name, tool_args)

        if key not in self._turn_cache:
            # Not a duplicate — mark for caching in after_tool_call
            return

        # Duplicate detected — return cached result without running the tool
        cached_result = self._turn_cache[key]
        logger.debug(
            "[ToolDedup] Suppressing duplicate call to %s (key=%s)", tool_name, key
        )

        ctx.extra["_skip_tool"] = True
        ctx.extra["_dedup_hit"] = True

        ctx.inputs.tool_result = cached_result
        if ToolMessage is not None:
            tool_call_id = getattr(ctx.inputs.tool_call, "id", "") or ""
            ctx.inputs.tool_msg = ToolMessage(
                content=cached_result,
                tool_call_id=tool_call_id,
            )

    async def after_tool_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Cache the result of a fresh call and update cross-turn counters."""
        # Skip if this was itself a cache hit (avoid double-counting)
        if ctx.extra.pop("_dedup_hit", False):
            return

        tool_name: str = ctx.inputs.tool_name or ""
        if tool_name not in _CACHEABLE_TOOLS:
            return

        tool_args: dict = ctx.inputs.tool_args or {}
        key = _cache_key(tool_name, tool_args)
        result: str = ctx.inputs.tool_result or ""

        # Store in per-turn cache
        self._turn_cache[key] = result

        # Increment cross-turn counter
        self._exec_counts[key] = self._exec_counts.get(key, 0) + 1
        count = self._exec_counts[key]

        # Inject warning if threshold reached for the first time
        if count >= self._warn_after and key not in self._warned_keys:
            self._warned_keys.add(key)
            self._inject_warning()

    # ------------------------------------------------------------------
    # Warning section helpers
    # ------------------------------------------------------------------

    def _inject_warning(self) -> None:
        if not self.system_prompt_builder:
            return
        # Build an aggregated warning covering all over-threshold keys
        lines = [
            "**Repeated tool-call notice**: the following tool+args combinations "
            "have been called multiple times and are unlikely to return new information:",
        ]
        for k in self._warned_keys:
            tname = k.split(":")[0]
            n = self._exec_counts.get(k, 0)
            lines.append(f"- `{tname}` (fingerprint `{k}`) called {n}× already")
        lines.append(
            "Do not call these again unless you have changed the inputs. "
            "Use the results you already have and proceed."
        )
        body = "\n".join(lines)
        self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={"cn": body, "en": body},
                priority=_WARNING_PRIORITY,
            )
        )
