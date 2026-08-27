# coding: utf-8
"""Shared context-window resolution for JiuWenSwarm model-facing surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openjiuwen.core.context_engine.context.context_utils import ContextUtils


def parse_positive_int(value: Any) -> int | None:
    """Return a positive integer, accepting values loaded from YAML."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
    """Resolve a model window with the JiuWenSwarm precedence contract.

    Priority:
    global ``context_window_tokens`` -> selected model ``context_window`` ->
    explicit model mapping -> core automatic lookup -> core default.

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

    fallback = global_window if global_window is not None else configured_model_window
    return ContextUtils.resolve_context_max(
        model_name=model_name,
        fallback_context_window_tokens=fallback,
        model_context_window_tokens=_normalize_model_window_mapping(
            config.get("model_context_window_tokens")
        ),
    )


__all__ = ["parse_positive_int", "resolve_context_window_tokens"]
