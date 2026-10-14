# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Academic-format rail for automated research paper generation.

Outputs that look like a paper draft (contain an abstract/introduction
heading) are checked for required sections; missing sections trigger an
injected "must complete" note so writing stages self-correct.

Config (``paper_guard`` section in ``config.yaml``)::

    paper_guard:
      enabled: true
"""

from __future__ import annotations

import logging
import re
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_REQUIRED_SECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("abstract", re.compile(r"^#{1,4}\s*(abstract|摘\s*要)", re.IGNORECASE | re.MULTILINE)),
    ("introduction", re.compile(r"^#{1,4}\s*(introduction|引\s*言)", re.IGNORECASE | re.MULTILINE)),
    ("method", re.compile(r"^#{1,4}\s*(method|methodology|方\s*法)", re.IGNORECASE | re.MULTILINE)),
    ("experiment", re.compile(r"^#{1,4}\s*(experiment|实验)", re.IGNORECASE | re.MULTILINE)),
    ("conclusion", re.compile(r"^#{1,4}\s*(conclusion|结\s*论)", re.IGNORECASE | re.MULTILINE)),
    ("references", re.compile(r"^#{1,4}\s*(references|参考文献)", re.IGNORECASE | re.MULTILINE)),
)

_LOOKS_LIKE_PAPER = re.compile(r"(abstract|摘\s*要|introduction|引\s*言)", re.IGNORECASE)
_HAS_HEADING = re.compile(r"^#{1,4}\s+\S+", re.MULTILINE)


class AcademicFormatRail(DeepAgentRail):
    """Require complete paper sections on paper-looking outputs."""

    priority: int = 70

    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self._enabled = enabled

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled:
            return
        try:
            output = _output_text(ctx)
            if not output or not _HAS_HEADING.search(output):
                return
            if not _LOOKS_LIKE_PAPER.search(output):
                return
            missing = [label for label, pat in _REQUIRED_SECTIONS if not pat.search(output)]
            if missing:
                _inject(
                    ctx,
                    "PAPER_GUARD[academic]: paper draft is missing required sections: "
                    + ", ".join(missing)
                    + "; complete them before finalizing.",
                )
            if re.search(r"\[\d+\]", output) and not re.search(
                r"(references|参考文献)", output, re.IGNORECASE,
            ):
                _inject(ctx, "PAPER_GUARD[academic]: citations present but the "
                             "references section is missing.")
        except Exception as exc:
            logger.warning("[PaperGuard] academic-format check skipped: %s", exc)


def _output_text(ctx: AgentCallbackContext) -> str:
    for attr in ("output", "response", "result", "text"):
        val = getattr(ctx, attr, None)
        if isinstance(val, str) and val:
            return val
        for sub in ("message", "text", "content"):
            sub_val = getattr(val, sub, None)
            if isinstance(sub_val, str) and sub_val:
                return sub_val
    return ""


def _inject(ctx: AgentCallbackContext, note: str) -> None:
    for attr in ("notes", "warnings", "system_notes"):
        lst = getattr(ctx, attr, None)
        if isinstance(lst, list):
            lst.append(note)
            logger.info("%s", note)
            return
    if hasattr(ctx, "__dict__"):
        for attr in vars(ctx):
            if isinstance(getattr(ctx, attr, None), list):
                getattr(ctx, attr).append(note)
                break
    logger.info("%s", note)