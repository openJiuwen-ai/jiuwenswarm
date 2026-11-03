# Gateway 侧副本，源文件 jiuwenswarm/common/context_window.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
"""Shared context-window resolution for Gateway model-facing surfaces."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

MAX_CONTEXT_WINDOW_TOKENS = 2**53 - 1
_CONTEXT_WINDOW_PATTERN = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?)\s*(tokens?|k(?:i?b)?|m(?:i?b)?)?$",
    re.IGNORECASE,
)
_CONTEXT_WINDOW_UNIT_MULTIPLIERS = {
    "token": 1,
    "tokens": 1,
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mi": 1024 * 1024,
    "mib": 1024 * 1024,
}


def parse_positive_int(value: Any) -> int | None:
    """Parse a positive token count with optional case-insensitive K/M units.

    Context values come from YAML, environment variables, and the settings UI.
    Accepting ``256k``, ``256 K``, ``1m``, and plain token counts here keeps all
    those entry points on the same parsing path.
    """
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, int):
        return value if 0 < value <= MAX_CONTEXT_WINDOW_TOKENS else None

    if isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
        return parsed if 0 < parsed <= MAX_CONTEXT_WINDOW_TOKENS else None

    normalized = str(value).strip().replace(",", "")
    match = _CONTEXT_WINDOW_PATTERN.fullmatch(normalized)
    if not match:
        return None

    try:
        amount = Decimal(match.group(1))
        multiplier = _CONTEXT_WINDOW_UNIT_MULTIPLIERS.get(
            (match.group(2) or "tokens").lower()
        )
        if multiplier is None:
            return None
        parsed_decimal = amount * multiplier
        if parsed_decimal != parsed_decimal.to_integral_value():
            return None
        parsed = int(parsed_decimal)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 < parsed <= MAX_CONTEXT_WINDOW_TOKENS else None


__all__ = [
    "MAX_CONTEXT_WINDOW_TOKENS",
    "parse_positive_int",
]
