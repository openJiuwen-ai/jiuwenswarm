from __future__ import annotations

from pathlib import Path

from .vocabulary import Finding


def review_findings(findings: list[Finding], skill_dir: Path, *, reviewer=None) -> tuple[list[Finding], bool]:
    if reviewer is None:
        return list(findings), False
    try:
        return list(reviewer(findings, skill_dir)), True
    except Exception:
        return list(findings), False
