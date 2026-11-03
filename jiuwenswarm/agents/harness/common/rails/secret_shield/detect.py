# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure candidate detection for secret-shield (no IO, no state)."""

from __future__ import annotations

import re

from jiuwenswarm.agents.harness.common.rails.secret_shield.vault import (
    MAX_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
)

MAX_CANDIDATES = 200

# Values with well-known credential prefixes carry enough shape to stand alone.
_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)

# Key/value forms keep the value in group 1.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"private[_-]?key|authorization)"
    # Backslashes are separators here: tool results often serialize line breaks
    # as literal ``\\n`` sequences, and a value must not absorb the next line.
    r"(?![A-Za-z0-9])[\"']?\s*[:=]\s*[\"']?([^\s\"'`,;<>\[\]{}\\]+)"
)

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]{8,}=*)")

# Noise shapes that look secret-like but are not.
_EXCLUDED_PREFIXES = ("data:", "http://", "https://")
_PURE_HEX_HASH = re.compile(r"^[0-9a-fA-F]{32,}$")


def looks_like_secret(value: str) -> bool:
    """Apply length and noise gates to one candidate value."""
    if not isinstance(value, str):
        return False
    text = value.strip().strip("\"'")
    if not MIN_SECRET_LENGTH <= len(text) <= MAX_SECRET_LENGTH:
        return False
    lowered = text.lower()
    if lowered.startswith(_EXCLUDED_PREFIXES):
        return False
    # Long pure-hex digests are usually content hashes, not credentials.
    if _PURE_HEX_HASH.fullmatch(text):
        return False
    return True


def collect_candidates(text: str) -> list[str]:
    """Extract ordered, de-duplicated secret candidates from untrusted text."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        if len(found) >= MAX_CANDIDATES:
            return
        cleaned = value.strip().strip("\"'").rstrip(".,;:")
        if cleaned in seen or not looks_like_secret(cleaned):
            return
        seen.add(cleaned)
        found.append(cleaned)

    for pattern in _PREFIX_PATTERNS:
        for match in pattern.finditer(text):
            _add(match.group(0))
    for match in _ASSIGNMENT_PATTERN.finditer(text):
        _add(match.group(1))
    for match in _BEARER_PATTERN.finditer(text):
        _add(match.group(1))
    return found


__all__ = ["MAX_CANDIDATES", "collect_candidates", "looks_like_secret"]
