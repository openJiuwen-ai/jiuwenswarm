"""Integer query/param parsing copied from AgentServer control responses."""

from __future__ import annotations

from typing import Any


def parse_int_param(
    params: dict[str, Any] | None,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """宽松解析整型参数（与 Web fallback 一致：int/整型 float/数字字符串）。

    非法值回落到 ``default``，随后夹取到 ``[minimum, maximum]``。
    """
    value = default
    raw = (params or {}).get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    value = max(minimum, min(value, maximum))
    return value
