# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Output format reminder rail.

Reads the task description file and extracts the output-format relevant
content — the final paragraph(s) of the prose body and any fenced code
blocks that show expected JSON/CSV/YAML structure.  The extracted snippet
is pinned as a permanent system-prompt section so the agent always has a
concise reminder of exactly what it needs to produce, even after context
compression has removed the original task description from the conversation
history.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection

logger = logging.getLogger(__name__)

_SECTION_NAME = "output_format_reminder"
_PRIORITY = 14          # after INTRO (10) and task_description (12), before SYSTEM (15)

# Fenced code blocks whose language tag suggests structured output
_OUTPUT_CODE_RE = re.compile(
    r"```(?:json|csv|tsv|yaml|yml|xml|toml|txt|text|plaintext)?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Sentence-level signals that strongly indicate output format instructions.
# Kept to format-specific terms only — generic words like "write" / "file" /
# "path" appear in nearly every paragraph and would make the filter match
# everything.
_FORMAT_SIGNAL_RE = re.compile(
    r"\b(output|format|csv|tsv|json|yaml|yml|xml|html|markdown|"
    r"plain.?text|stdout)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------
# Extraction helpers
# ------------------------------------------------------------------

def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter delimited by leading/trailing '---'."""
    stripped = text.strip()
    if not stripped.startswith("---"):
        return stripped
    # Find closing ---
    end = stripped.find("\n---", 3)
    if end == -1:
        return stripped
    return stripped[end + 4:].strip()


def _last_paragraphs(body: str, n: int = 2) -> list[str]:
    """Return the last *n* non-empty paragraphs from *body*."""
    paras = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    return paras[-n:] if paras else []


def _output_code_blocks(body: str, max_blocks: int = 2) -> list[str]:
    """Return fenced code blocks that are likely showing output format examples."""
    blocks = _OUTPUT_CODE_RE.findall(body)
    return [b.strip() for b in blocks[:max_blocks] if b.strip()]


def _has_format_signal(text: str) -> bool:
    return bool(_FORMAT_SIGNAL_RE.search(text))


def _build_snippet(body: str, max_chars: int) -> str | None:
    """Build a concise output-format snippet from the task body."""
    paragraphs = _last_paragraphs(body, n=2)
    code_blocks = _output_code_blocks(body, max_blocks=2)

    # Filter paragraphs: only include those with output-format signals
    relevant_paras = [p for p in paragraphs if _has_format_signal(p)]

    if not relevant_paras and not code_blocks:
        return None

    parts: list[str] = []
    if relevant_paras:
        parts.append("\n\n".join(relevant_paras))
    for block in code_blocks:
        parts.append(f"```\n{block}\n```")

    snippet = "\n\n".join(parts)
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rsplit("\n", 1)[0] + "\n…"
    return snippet


class OutputFormatRail(DeepAgentRail):
    """Pin a concise output-format reminder extracted from the task file.

    On each ``before_invoke`` the rail reads the task description file,
    strips the YAML front-matter, and extracts two things:

    * The **last 1–2 paragraphs** of the prose body — these almost always
      describe the required output (file path, format, constraints).
    * Any **fenced code blocks** using a structured format tag (json, csv,
      yaml, xml …) — these usually show the expected output schema.

    The extracted content is injected as a ``PromptSection`` at priority 14,
    sitting just before the general SYSTEM section (15) and after the full
    task description (12) so the agent reads the format reminder as one of
    the first things in every system prompt, regardless of context
    compression.

    If the task file is missing or contains nothing format-relevant the
    section is simply not added — no crash, no side effect.

    Priority 4 — runs early in the rail chain alongside TaskDescriptionRail,
    but is entirely independent of it.
    """

    priority = 4

    def __init__(self, task_path: str = "/app/task.md", max_chars: int = 800) -> None:
        super().__init__()
        self._task_path = task_path
        self._max_chars = max_chars
        self._injected: bool = False
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
    # Hooks
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        if self.system_prompt_builder:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
        self._injected = False
        self._try_inject()

    async def before_model_call(self, ctx: AgentCallbackContext, **kwargs: Any) -> None:
        if not self._injected:
            self._try_inject()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_inject(self) -> None:
        if not self.system_prompt_builder:
            return
        raw = self._read_file()
        if raw is None:
            return
        body = _strip_frontmatter(raw)
        snippet = _build_snippet(body, self._max_chars)
        if snippet is None:
            logger.debug("[OutputFormat] No format-relevant content found in %s", self._task_path)
            return
        content = f"# Required Output Format\n\n{snippet}"
        self.system_prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={"cn": content, "en": content},
                priority=_PRIORITY,
            )
        )
        self._injected = True
        logger.debug("[OutputFormat] Injected output-format reminder from %s", self._task_path)

    def _read_file(self) -> str | None:
        try:
            path = Path(self._task_path)
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                # Normalize CRLF so frontmatter/paragraph/code-block parsing
                # behaves the same on files written on Windows.
                return text.replace("\r\n", "\n").strip()
        except Exception as exc:
            logger.warning("[OutputFormat] Could not read %s: %s", self._task_path, exc)
        return None
