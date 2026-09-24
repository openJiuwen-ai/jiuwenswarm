# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a stored OTLP payload is read, and when a reader may project it.

Readers hand the viewer a record's parsed OTLP only when it parses as strict,
finite JSON of bounded nesting with a ``resourceSpans`` array; anything else
reaches the viewer as raw bytes and is never projected. Retention decides
which records the viewer groups and pages by the same rule, so it lives here
once for both.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_JSON_NESTING_DEPTH = 256
MAX_SAFE_INTEGER = (1 << 53) - 1

_DECIMAL_BIGINT = re.compile(r"[+-]?[0-9]+")
_PREFIXED_BIGINT = re.compile(r"0([xXoObB])([0-9a-zA-Z]+)")
_BIGINT_RADIX = {"x": 16, "o": 8, "b": 2}


def strict_otlp_payload(raw_json: bytes) -> dict[str, Any]:
    """Parse strict finite JSON and validate the minimum OTLP envelope shape.

    Raises:
        ValueError: When the bytes are not strict JSON of the OTLP envelope
            shape, including when they are not valid UTF-8.
        RecursionError: When the decoder itself runs out of stack.
    """

    validate_json_nesting(raw_json)

    def _reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    payload = json.loads(
        raw_json,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(payload, dict):
        raise ValueError("OTLP record must be a JSON object")
    if not isinstance(payload.get("resourceSpans"), list):
        raise ValueError("OTLP record resourceSpans must be an array")
    return payload


def parse_otlp_payload(raw_json: bytes) -> dict[str, Any] | None:
    """Return a payload parsed by ``strict_otlp_payload``, or None when a reader rejects it."""
    try:
        return strict_otlp_payload(raw_json)
    except (RecursionError, TypeError, ValueError, OverflowError):
        return None


def js_bigint(value: Any) -> int | None:
    """Convert a parsed JSON value the way the viewer's ``BigInt(value)`` does.

    The viewer reads integers with JavaScript's ``BigInt``: a boolean counts as
    0 or 1, a number must be integral, and a string is trimmed, empty meaning
    0, decimal with an optional sign or hexadecimal, octal or binary with a
    prefix. A JSON number beyond 2^53 has already lost precision in the
    viewer's parser, so it is converted through a double here as well.

    Returns:
        The integer, or None where ``BigInt`` throws.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER:
            return value
        try:
            return js_bigint(float(value))
        except OverflowError:
            return None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return 0
    if _DECIMAL_BIGINT.fullmatch(text) is not None:
        return int(text)
    prefixed = _PREFIXED_BIGINT.fullmatch(text)
    if prefixed is None:
        return None
    try:
        return int(prefixed.group(2), _BIGINT_RADIX[prefixed.group(1).lower()])
    except ValueError:
        return None


def int64_attribute_value(value: Any) -> int | None:
    """Read an OTLP AnyValue's intValue arm as the viewer's readInt64Attribute does."""
    if not isinstance(value, dict) or "intValue" not in value:
        return None
    return js_bigint(value["intValue"])


def validate_json_nesting(raw_json: bytes) -> None:
    """Reject excessive JSON nesting without decoding or recursive traversal."""
    # A payload with fewer opening brackets than the limit cannot nest deeper
    # than it. Counting is C-level, so the byte walk below only runs for the
    # rare payload that could actually exceed the limit.
    if raw_json.count(b"[") + raw_json.count(b"{") <= MAX_JSON_NESTING_DEPTH:
        return
    depth = 0
    in_string = False
    escaped = False
    for character in raw_json:
        if in_string:
            if escaped:
                escaped = False
            elif character == 0x5C:
                escaped = True
            elif character == 0x22:
                in_string = False
            continue
        if character == 0x22:
            in_string = True
            continue
        if character in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON nesting depth exceeds projection limit")
        elif character in (0x5D, 0x7D):
            depth -= 1


__all__ = [
    "MAX_JSON_NESTING_DEPTH",
    "MAX_SAFE_INTEGER",
    "int64_attribute_value",
    "js_bigint",
    "parse_otlp_payload",
    "strict_otlp_payload",
    "validate_json_nesting",
]
