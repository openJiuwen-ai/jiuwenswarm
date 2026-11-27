# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Sensitive data masking for log messages."""

from __future__ import annotations

import logging
import re

_SENSITIVE_MASK = "******"
_KV_SENSITIVE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|user[_-]?id|userid)"
    r"(?![A-Za-z0-9])(\s*[:=]\s*)([\"']?)[^,\s\"'\]\}]+([\"']?)"
)
_NAMED_SENSITIVE_KV_PATTERN = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_.-]*"
    r"(?:token|secret|password|passwd|pwd|api[_-]?key|authorization|"
    r"credential|private[_-]?key|user[_-]?id|userid)"
    r"[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
)
_BEARER_SENSITIVE_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]+=*")
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]


def sanitize_log_text(text: str) -> str:
    if not text:
        return text

    masked = text
    masked = _KV_SENSITIVE_PATTERN.sub(r"\1\2" f"{_SENSITIVE_MASK}", masked)
    masked = _NAMED_SENSITIVE_KV_PATTERN.sub(r"\1\2" f"{_SENSITIVE_MASK}" r"\2", masked)
    masked = _BEARER_SENSITIVE_PATTERN.sub(r"\1" f"{_SENSITIVE_MASK}", masked)
    for pattern in _SENSITIVE_PATTERNS:
        masked = pattern.sub(_SENSITIVE_MASK, masked)
    return masked


class SensitiveDataFilter(logging.Filter):
    """Mask sensitive data in all log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            record.msg = sanitize_log_text(message)
            record.args = ()
        except Exception:
            pass
        return True
