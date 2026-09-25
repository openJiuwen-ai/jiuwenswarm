from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .rules import HIGH_CONFIDENCE_CREDENTIAL_RULE_IDS, RULES
from .vocabulary import Finding, Severity, ThreatCategory

# ``.archive`` is product-managed: the TeamSkills-Hub install path writes a full
# copy of the skill's own content under ``.archive/versions/...``, so it is
# already represented by the scanned live copy (and still covered by the hash).
# Reporting it would grade every hub-installed benign skill HIGH and gate it.
_ARCHIVE_DIR = ".archive"
# Code-bearing dirs can hide an attacker payload the live tree never shows.
_CODE_BEARING_SKIP_DIRS = {".git", "node_modules", ".venv"}
# Bytecode derived from already-scanned source: worth surfacing, must not gate.
_DERIVED_SKIP_DIRS = {"__pycache__"}
_SKIP_DIRS = {_ARCHIVE_DIR} | _CODE_BEARING_SKIP_DIRS | _DERIVED_SKIP_DIRS
_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB per file
_TEXT_CHUNK = 4096

# Reason -> rule id. "skipped-dir" severity is resolved per path by
# ``_skipped_dir_severity``; every other reason is always HIGH.
_UNSCANNED_RULES: dict[str, str] = {
    "skipped-dir": "unscanned.skipped-dir",
    "too-large": "unscanned.too-large",
    "binary": "unscanned.binary",
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


def _skipped_dir_severity(rel_parts: tuple[str, ...]) -> Severity | None:
    """Severity for a file dropped by a skip dir, or ``None`` to emit nothing.

    Precedence: root ``.archive`` (product-managed, emit nothing) > code-bearing
    (HIGH) > derived bytecode (MEDIUM). A ``.archive`` at any non-root depth is
    NOT the product-managed archive, so it can hide a payload and is HIGH.
    """
    if rel_parts and rel_parts[0] == _ARCHIVE_DIR:
        return None
    if _ARCHIVE_DIR in rel_parts or any(
        part in _CODE_BEARING_SKIP_DIRS for part in rel_parts
    ):
        return Severity.HIGH
    if any(part in _DERIVED_SKIP_DIRS for part in rel_parts):
        return Severity.MEDIUM
    return None


def _unscanned_findings(skill_dir: Path) -> list[Finding]:
    """Surface every file the scan set dropped, so skipped content never vanishes."""
    findings: list[Finding] = []
    for path, reason in iter_skipped_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        if reason == "skipped-dir":
            severity = _skipped_dir_severity(path.relative_to(skill_dir).parts)
            if severity is None:
                continue
        else:
            severity = Severity.HIGH
        rule_id = _UNSCANNED_RULES[reason]
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
    """Cross-category escalation: credential-read + network-egress = exfil (EXTREME).

    Only HIGH-confidence credential access escalates. Generic env/config reads
    (``credential.env-read``/``credential.dotenv``) are ubiquitous in benign
    skills, so pairing them with any HTTP call would grade almost every Python
    skill EXTREME and train users to blind-approve.
    """
    has_high_confidence_cred = any(
        f.rule_id in HIGH_CONFIDENCE_CREDENTIAL_RULE_IDS for f in findings
    )
    has_network = any(f.category is ThreatCategory.NETWORK for f in findings)
    if has_high_confidence_cred and has_network:
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
