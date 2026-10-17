# coding: utf-8
"""Shared context-window resolution for JiuWenSwarm model-facing surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import re
from typing import Any

DEFAULT_CONTEXT_WINDOW_TOKENS = 256 * 1024
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


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _normalize_model_window_mapping(value: Any) -> dict[str, int] | None:
    mapping = _as_mapping(value)
    if not mapping:
        return None
    normalized: dict[str, int] = {}
    for model_name, window in mapping.items():
        if not isinstance(model_name, str):
            continue
        parsed = parse_positive_int(window)
        if parsed is not None:
            normalized[model_name] = parsed
    return normalized or None


def resolve_context_window_tokens(
    model_name: str | None,
    *,
    context_engine_config: Any = None,
    model_config_obj: Any = None,
    model_context_window_override: Any = None,
) -> int:
    """Resolve a model window without querying provider metadata.

    Priority:
    global ``context_window_tokens`` -> selected model ``context_window`` ->
    explicit model mapping -> fixed JiuwenSwarm default.

    ``model_context_window_override`` is used by runtime paths after the
    selected model has already been converted to ``ModelRequestConfig`` and
    its internal ``_source`` marker is no longer available.
    """
    config = _as_mapping(context_engine_config)
    if "context_engine_config" in config:
        config = _as_mapping(config.get("context_engine_config"))

    model_config = _as_mapping(model_config_obj)
    global_window = parse_positive_int(config.get("context_window_tokens"))

    configured_model_window = parse_positive_int(model_config.get("context_window"))
    if configured_model_window is None:
        configured_model_window = parse_positive_int(model_context_window_override)

    if global_window is not None:
        return global_window
    if configured_model_window is not None:
        return configured_model_window

    explicit_model_windows = (
        _normalize_model_window_mapping(config.get("model_context_window_tokens")) or {}
    )
    if isinstance(model_name, str):
        mapped_window = parse_positive_int(explicit_model_windows.get(model_name))
        if mapped_window is not None:
            return mapped_window

    return DEFAULT_CONTEXT_WINDOW_TOKENS


__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "MAX_CONTEXT_WINDOW_TOKENS",
    "parse_positive_int",
    "resolve_context_window_tokens",
]
