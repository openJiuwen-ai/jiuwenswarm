from __future__ import annotations

from pathlib import Path
from typing import Callable

from .vocabulary import Finding

ReviewerFn = Callable[[list[Finding], Path], list[Finding]]


def review_findings(
    findings: list[Finding],
    skill_dir: Path,
    *,
    reviewer: ReviewerFn | None = None,
) -> tuple[list[Finding], bool]:
    """Advisory LLM escalation. Returns (findings, escalated).

    ``reviewer`` is optional; when absent (or raising) the deterministic findings
    are returned unchanged and ``escalated`` is False. The deterministic grade is
    always the floor for gating.
    """
    if reviewer is None:
        return list(findings), False
    try:
        return list(reviewer(findings, skill_dir)), True
    except Exception:
        return list(findings), False
