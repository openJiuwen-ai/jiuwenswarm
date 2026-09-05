# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task Description Rail.

Reads a task description file from disk and pins its content as a dedicated
system-prompt section on every model call.  Because the section lives in the
system prompt rather than the conversation history it is never compressed
away, so the agent always has the original task visible regardless of how
long the conversation has grown.

Typical use-case: CI pipelines or evaluation harnesses where the task is
written to a file (e.g. ``/app/task.md``) before the agent starts.  The
path is configurable via ``task_description.path`` in config.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_SECTION_NAME = "task_description"

# Priority 12 — just after INTRO (10) and before SYSTEM (15), so the agent
# reads the task description immediately after learning who it is.
_SECTION_PRIORITY = 12


class TaskDescriptionRail(DeepAgentRail):
    """Pin a task-description file as a permanent system-prompt section.

    Priority 4 ensures this fires before RuntimePromptRail (5) so the
    section is present for every LLM call including the very first one.

    Lifecycle
    ---------
    * ``before_invoke``  — clears any previous section and attempts a fresh
      read at the start of each agent invocation.
    * ``before_model_call`` — retries the read/inject if the file was not
      available at invoke time (e.g. mounted asynchronously).
    """

    priority = 4

    def __init__(self, task_path: str) -> None:
        super().__init__()
        self._task_path = task_path
        self._injected: bool = False
        self.system_prompt_builder = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self.system_prompt_builder = None

    # ── rail hooks ───────────────────────────────────────────────────────────

    async def before_invoke(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Clear stale section and re-read the file at invocation start."""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self._injected = False
        self._try_inject()

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        """Retry injection if the file was not available at invoke time."""
        if not self._injected:
            self._try_inject()

    # ── internals ────────────────────────────────────────────────────────────

    def _try_inject(self) -> None:
        if self.system_prompt_builder is None:
            return
        content = self._read_file()
        if not content:
            # No usable file (missing or empty) → do not mark injected, so
            # before_model_call keeps retrying until the file is populated.
            return
        body = f"# Task Description\n\n{content}"
        self.system_prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={"cn": body, "en": body},
                priority=_SECTION_PRIORITY,
            )
        )
        self._injected = True
        logger.debug(
            "[TaskDescriptionRail] Injected task description from %s (%d chars)",
            self._task_path,
            len(content),
        )

    def _read_file(self) -> str | None:
        try:
            path = Path(self._task_path)
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning(
                "[TaskDescriptionRail] Could not read task file %s: %s",
                self._task_path,
                exc,
            )
        return None


__all__ = ["TaskDescriptionRail"]
