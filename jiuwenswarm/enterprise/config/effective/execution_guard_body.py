# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""执行护栏 body 校验。

AgentServer 套用企业配置前使用。规则与 Manager、Gateway 的副本一致：
``manager_server.schemas.execution_guard_body``、
``agent_gateway.enterprise.config.manager_config.schemas.execution_guard_body``。
"""

from __future__ import annotations

from typing import Any

_TOP_FIELDS = frozenset({"model_anomaly_detection_rail", "circuit_breaker"})
_RAIL_FIELDS = frozenset({
    "enabled",
    "max_retries",
    "transient_max_retries",
    "repeat_min_pattern_chars",
    "repeat_max_pattern_chars",
    "repeat_min_count",
    "repeat_min_total_chars",
    "repeat_window_chars",
    "single_char_repeat_count",
    "tool_loop_compact",
})
_COMPACT_FIELDS = frozenset({
    "enabled",
    "consecutive_threshold",
    "bailout_threshold",
})
_BREAKER_FIELDS = frozenset({
    "enabled",
    "warning_threshold",
    "critical_threshold",
    "global_breaker_threshold",
    "unknown_tool_threshold",
})

DEFAULT_EXECUTION_GUARD_BODY: dict[str, Any] = {
    "model_anomaly_detection_rail": {
        "enabled": True,
        "max_retries": 2,
        "transient_max_retries": 3,
        "repeat_min_pattern_chars": 2,
        "repeat_max_pattern_chars": 64,
        "repeat_min_count": 6,
        "repeat_min_total_chars": 160,
        "repeat_window_chars": 1024,
        "single_char_repeat_count": 90,
        "tool_loop_compact": {
            "enabled": True,
            "consecutive_threshold": 4,
            "bailout_threshold": 3,
        },
    },
    "circuit_breaker": {
        "enabled": False,
        "warning_threshold": 10,
        "critical_threshold": 20,
        "global_breaker_threshold": 30,
        "unknown_tool_threshold": 10,
    },
}


def validate_execution_guard_body(body: Any) -> dict[str, Any]:
    """校验并返回一份只含允许字段的 body。非法时抛 ``ValueError``。"""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    _require_exact_fields(body, _TOP_FIELDS, "body")
    return {
        "model_anomaly_detection_rail": _validate_rail(body["model_anomaly_detection_rail"]),
        "circuit_breaker": _validate_breaker(body["circuit_breaker"]),
    }


def _validate_rail(value: Any) -> dict[str, Any]:
    rail = _require_object(value, "model_anomaly_detection_rail")
    _require_exact_fields(rail, _RAIL_FIELDS, "model_anomaly_detection_rail")
    compact = _require_object(rail["tool_loop_compact"], "tool_loop_compact")
    _require_exact_fields(compact, _COMPACT_FIELDS, "tool_loop_compact")

    repeat_max = _require_int(rail["repeat_max_pattern_chars"], "repeat_max_pattern_chars", 1)
    repeat_min = _require_int(rail["repeat_min_pattern_chars"], "repeat_min_pattern_chars", 1)
    if repeat_max < repeat_min:
        raise ValueError("repeat_max_pattern_chars must be >= repeat_min_pattern_chars")
    repeat_count = _require_int(rail["repeat_min_count"], "repeat_min_count", 2)
    repeat_total = _require_int(rail["repeat_min_total_chars"], "repeat_min_total_chars", 1)
    single_char = _require_int(rail["single_char_repeat_count"], "single_char_repeat_count", 2)
    min_window = max(repeat_max * repeat_count, repeat_total, single_char)
    window = _require_int(rail["repeat_window_chars"], "repeat_window_chars", 1)
    if window < min_window:
        raise ValueError(
            "repeat_window_chars must be >= "
            f"{min_window}"
        )
    return {
        "enabled": _require_bool(rail["enabled"], "model_anomaly_detection_rail.enabled"),
        "max_retries": _require_int(rail["max_retries"], "max_retries", 0),
        "transient_max_retries": _require_int(
            rail["transient_max_retries"], "transient_max_retries", 0
        ),
        "repeat_min_pattern_chars": repeat_min,
        "repeat_max_pattern_chars": repeat_max,
        "repeat_min_count": repeat_count,
        "repeat_min_total_chars": repeat_total,
        "repeat_window_chars": window,
        "single_char_repeat_count": single_char,
        "tool_loop_compact": {
            "enabled": _require_bool(compact["enabled"], "tool_loop_compact.enabled"),
            "consecutive_threshold": _require_int(
                compact["consecutive_threshold"], "consecutive_threshold", 2
            ),
            "bailout_threshold": _require_int(
                compact["bailout_threshold"], "bailout_threshold", 0
            ),
        },
    }


def _validate_breaker(value: Any) -> dict[str, Any]:
    breaker = _require_object(value, "circuit_breaker")
    _require_exact_fields(breaker, _BREAKER_FIELDS, "circuit_breaker")
    warning = _require_int(breaker["warning_threshold"], "warning_threshold", 1)
    critical = _require_int(breaker["critical_threshold"], "critical_threshold", 1)
    if critical < warning:
        raise ValueError("critical_threshold must be >= warning_threshold")
    return {
        "enabled": _require_bool(breaker["enabled"], "circuit_breaker.enabled"),
        "warning_threshold": warning,
        "critical_threshold": critical,
        "global_breaker_threshold": _require_int(
            breaker["global_breaker_threshold"], "global_breaker_threshold", 1
        ),
        "unknown_tool_threshold": _require_int(
            breaker["unknown_tool_threshold"], "unknown_tool_threshold", 1
        ),
    }


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _require_exact_fields(value: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    missing = sorted(allowed - set(value))
    if missing:
        raise ValueError(f"{where} is missing fields: {', '.join(missing)}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{where} contains unknown fields: {', '.join(unknown)}")


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _require_int(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value
