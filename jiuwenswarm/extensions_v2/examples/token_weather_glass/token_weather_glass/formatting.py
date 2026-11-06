# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Display observed usage with the host context popover's numeric rules."""

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP


def _one_decimal(value: float) -> str:
    """Match the host's Number(value.toFixed(1)), including binary rounding ties."""
    rounded = Decimal.from_float(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(Decimal(str(float(rounded))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_context_tokens(value: int | float) -> str:
    """Use decimal K and omit a zero fractional digit."""
    if value >= 1_000:
        return f"{_one_decimal(value / 1_000)}K"
    return f"{value:g}"


def format_context_limit(value: int | float) -> str:
    """Use the host's binary K/M conversion for context limits."""
    if value < 1_000:
        return f"{value:g}"
    scale, suffix = (1024 * 1024, "M") if value >= 1024 * 1024 else (1024, "K")
    return f"{_one_decimal(value / scale)}{suffix}"


def context_percent(context: Mapping[str, int | float]) -> float | None:
    """Read unrounded occupancy from known counts or the reported percentage."""
    if "tokens" in context and "window" in context:
        return context["tokens"] / context["window"] * 100
    if "percent" in context:
        return float(context["percent"])
    return None


def format_context_percent(context: Mapping[str, int | float]) -> str | None:
    """Derive precise occupancy from known counts, preserving unknown readings."""
    percent = context_percent(context)
    return f"{_one_decimal(percent)}%" if percent is not None else None
