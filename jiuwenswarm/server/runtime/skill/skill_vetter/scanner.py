from __future__ import annotations

import hashlib
from pathlib import Path

from .rules import RULES
from .vocabulary import Finding, Severity, ThreatCategory

_SKIP_DIRS = {".archive", ".git", "__pycache__", "node_modules", ".venv"}
_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB per file
_TEXT_CHUNK = 4096


def iter_scannable_files(skill_dir: Path) -> list[Path]:
    """Return regular text files under *skill_dir*, skipping known non-code dirs."""
    out: list[Path] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(skill_dir).parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            with path.open("rb") as fh:
                chunk = fh.read(_TEXT_CHUNK)
            if b"\x00" in chunk:
                continue
        except OSError:
            continue
        out.append(path)
    return out


def compute_content_hash(skill_dir: Path) -> str:
    """SHA-256 over the exact byte set the scanner reads, keyed by relative path."""
    h = hashlib.sha256()
    for path in iter_scannable_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
        h.update(b"\x00")
    return h.hexdigest()


def _scan_files(skill_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_scannable_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule in RULES:
            findings.extend(rule.scan(rel, text))
    return findings


def _combination_findings(findings: list[Finding]) -> list[Finding]:
    """Cross-category escalation: credential-read + network-egress = exfil (EXTREME)."""
    cats = {f.category for f in findings}
    if ThreatCategory.CREDENTIAL_THEFT in cats and ThreatCategory.NETWORK in cats:
        return [
            Finding(
                category=ThreatCategory.CREDENTIAL_THEFT,
                severity=Severity.EXTREME,
                file="",
                line=0,
                evidence="credential read combined with network egress",
                rule_id="combination.exfil",
            )
        ]
    return []


def scan_skill(skill_dir: Path) -> list[Finding]:
    """Deterministic scan: per-file rules + cross-category combination pass."""
    findings = _scan_files(skill_dir)
    findings.extend(_combination_findings(findings))
    return findings
