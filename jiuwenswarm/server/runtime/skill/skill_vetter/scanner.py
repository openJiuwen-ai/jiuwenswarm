from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .rules import RULES
from .vocabulary import Finding, Severity, ThreatCategory

_SKIP_DIRS = {".archive", ".git", "__pycache__", "node_modules", ".venv"}
_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB per file
_TEXT_CHUNK = 4096

_UNSCANNED_RULES: dict[str, tuple[Severity, str]] = {
    "skipped-dir": (Severity.HIGH, "unscanned.skipped-dir"),
    "too-large": (Severity.HIGH, "unscanned.too-large"),
    "binary": (Severity.MEDIUM, "unscanned.binary"),
}


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


def _iter_all_files(skill_dir: Path) -> list[Path]:
    """Every file under *skill_dir*, sorted by relative POSIX path.

    Symlinked files are included (their target bytes are read), but symlinked
    directories are not descended into — ``os.walk(followlinks=False)`` avoids
    symlink loops while still listing the link entries.
    """
    paths: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(skill_dir, followlinks=False):
        for filename in filenames:
            path = Path(dirpath) / filename
            # Only regular files: FIFOs/sockets/device nodes would block on read.
            if path.is_file():
                paths.append(path)
    paths.sort(key=lambda p: p.relative_to(skill_dir).as_posix())
    return paths


def compute_content_hash(skill_dir: Path) -> str:
    """SHA-256 over every file under *skill_dir*, keyed by relative path.

    Zero exclusions: skip dirs, the size cap and binary detection are scanner
    policy, not identity. A change to any byte anywhere — including files the
    scanner would never read — must change the hash, otherwise a previously
    vetted skill could be updated in place without re-vetting.
    """
    h = hashlib.sha256()
    for path in _iter_all_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
        h.update(b"\x00")
    return h.hexdigest()


def iter_skipped_files(skill_dir: Path) -> list[tuple[Path, str]]:
    """Files the scanner excludes, as ``(path, reason)``.

    Each file is classified exactly once, in precedence order:
    ``skipped-dir`` > ``too-large`` > ``binary``.
    """
    out: list[tuple[Path, str]] = []
    for dirpath, _dirnames, filenames in os.walk(skill_dir, followlinks=False):
        for filename in filenames:
            path = Path(dirpath) / filename
            # Only regular files: non-regular entries are neither hashed nor
            # reported as skipped (reading them could block indefinitely).
            if not path.is_file():
                continue
            rel_parts = path.relative_to(skill_dir).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                out.append((path, "skipped-dir"))
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    out.append((path, "too-large"))
                    continue
                with path.open("rb") as fh:
                    chunk = fh.read(_TEXT_CHUNK)
                if b"\x00" in chunk:
                    out.append((path, "binary"))
                    continue
            except OSError:
                continue
    out.sort(key=lambda item: item[0].relative_to(skill_dir).as_posix())
    return out


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


def _unscanned_findings(skill_dir: Path) -> list[Finding]:
    """Surface every file the scan set dropped, so skipped content never vanishes."""
    findings: list[Finding] = []
    for path, reason in iter_skipped_files(skill_dir):
        severity, rule_id = _UNSCANNED_RULES[reason]
        rel = path.relative_to(skill_dir).as_posix()
        findings.append(
            Finding(
                category=ThreatCategory.OBFUSCATION,
                severity=severity,
                file=rel,
                line=0,
                evidence=f"unscanned ({reason})",
                rule_id=rule_id,
            )
        )
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
    """Deterministic scan: per-file rules + unscanned findings + combination pass."""
    findings = _scan_files(skill_dir)
    findings.extend(_unscanned_findings(skill_dir))
    findings.extend(_combination_findings(findings))
    return findings
