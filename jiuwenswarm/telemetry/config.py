"""Telemetry configuration loaded from environment variables and config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Callable, TypeVar

from jiuwenswarm.common.config import get_config


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    exporter: str = "none"
    endpoint: str = "http://localhost:4317"
    protocol: str = "grpc"
    headers: dict[str, str] = field(default_factory=dict)
    traces_exporter: str = "none"
    traces_endpoint: str = "http://localhost:4317"
    traces_protocol: str = "grpc"
    traces_headers: dict[str, str] = field(default_factory=dict)
    metrics_exporter: str = "none"
    metrics_endpoint: str = "http://localhost:4317"
    metrics_protocol: str = "grpc"
    metrics_headers: dict[str, str] = field(default_factory=dict)
    logs_exporter: str = "none"
    logs_endpoint: str = "http://localhost:4317"
    logs_protocol: str = "grpc"
    logs_headers: dict[str, str] = field(default_factory=dict)
    service_name: str = "jiuwenclaw"
    sample_rate: float = 1.0
    max_attributes: int = 128
    attribute_value_max_length: int = 10240
    redact_prompts: bool = False
    redact_completions: bool = False
    log_messages: bool = True
    claw_id: str | None = None
    session_stuck_threshold_ms: float = 300000.0
    session_stuck_check_interval_s: float = 30.0

    @property
    def unified_mode(self) -> bool:
        return self.enabled


def _nonempty(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_headers_with_validity(value: Any) -> tuple[dict[str, str], bool]:
    if isinstance(value, dict):
        mapping_headers = {
            str(key).strip(): str(header_value).strip()
            for key, header_value in value.items()
            if str(key).strip()
        }
        return mapping_headers, bool(mapping_headers)
    if isinstance(value, str):
        if not value.strip():
            return {}, False
        string_headers: dict[str, str] = {}
        for item in value.split(","):
            if "=" not in item:
                continue
            key, header_value = item.split("=", 1)
            key = key.strip()
            if key:
                string_headers[key] = header_value.strip()
        return string_headers, bool(string_headers)
    return {}, False


def _parse_headers(value: Any) -> dict[str, str]:
    return _parse_headers_with_validity(value)[0]


def _yaml_signal_value(yaml_cfg: dict[str, Any], signal: str, key: str) -> Any:
    flat_key = f"{signal}_{key}"
    flat_value = yaml_cfg.get(flat_key)
    if _nonempty(flat_value) is not None:
        return flat_value
    signal_cfg = yaml_cfg.get(signal)
    return signal_cfg.get(key) if isinstance(signal_cfg, dict) else None


def _yaml_signal_headers_value(yaml_cfg: dict[str, Any], signal: str) -> Any:
    flat_key = f"{signal}_headers"
    flat_value = yaml_cfg.get(flat_key)
    _, flat_is_valid = _parse_headers_with_validity(flat_value)
    if flat_is_valid:
        return flat_value
    signal_cfg = yaml_cfg.get(signal)
    return signal_cfg.get("headers") if isinstance(signal_cfg, dict) else None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int:
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _parse_float(value: Any) -> float | None:
    try:
        parsed = float(value.strip() if isinstance(value, str) else value)
        return parsed if isfinite(parsed) else None
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (not isfinite(value) or not value.is_integer()):
        return None
    try:
        return int(value.strip() if isinstance(value, str) else value)
    except (OverflowError, TypeError, ValueError):
        return None


T = TypeVar("T")


def _coerce_value(
    env_key: str,
    yaml_value: Any,
    default: T,
    parser: Callable[[Any], T | None],
) -> T:
    for value in (os.getenv(env_key), yaml_value, default):
        if _nonempty(value) is None:
            continue
        parsed = parser(value)
        if parsed is not None:
            return parsed
    return default


def _string_value(env_key: str, yaml_value: Any, default: str) -> str:
    for value in (os.getenv(env_key), yaml_value, default):
        value = _nonempty(value)
        if value is not None:
            return str(value).strip()
    return default


def _headers_value(
    env_key: str, yaml_value: Any, fallback: dict[str, str]
) -> dict[str, str]:
    for value in (os.getenv(env_key), yaml_value, fallback):
        headers, valid = _parse_headers_with_validity(value)
        if valid:
            return headers
    return {}


def _normalize(value: str, default: str) -> str:
    return value.strip().lower() or default


def _load_yaml_config() -> dict[str, Any]:
    try:
        config = get_config()
        telemetry = config.get("telemetry") if isinstance(config, dict) else None
        return telemetry if isinstance(telemetry, dict) else {}
    except Exception:
        return {}


def load_telemetry_config() -> TelemetryConfig:
    """Load telemetry settings, with environment variables taking precedence."""
    yaml_cfg = _load_yaml_config()
    session_cfg = yaml_cfg.get("session")
    session_cfg = session_cfg if isinstance(session_cfg, dict) else {}

    exporter = _normalize(
        _string_value("OTEL_EXPORTER_TYPE", yaml_cfg.get("exporter"), "none"), "none"
    )
    endpoint = _string_value(
        "OTEL_EXPORTER_OTLP_ENDPOINT", yaml_cfg.get("endpoint"), "http://localhost:4317"
    )
    protocol = _normalize(
        _string_value("OTEL_EXPORTER_OTLP_PROTOCOL", yaml_cfg.get("protocol"), "grpc"),
        "grpc",
    )
    headers = _headers_value("OTEL_EXPORTER_OTLP_HEADERS", yaml_cfg.get("headers"), {})

    def signal_value(signal: str, key: str, env_key: str, common: str, default: str) -> str:
        signal_yaml = _yaml_signal_value(yaml_cfg, signal, key)
        signal_yaml = signal_yaml if _nonempty(signal_yaml) is not None else common
        return _string_value(env_key, signal_yaml, default)

    traces_exporter = _normalize(
        signal_value("traces", "exporter", "OTEL_TRACES_EXPORTER", exporter, exporter), exporter
    )
    traces_endpoint = signal_value(
        "traces", "endpoint", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint, endpoint
    )
    traces_protocol = _normalize(
        signal_value("traces", "protocol", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", protocol, protocol),
        protocol,
    )
    traces_headers = _headers_value(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        _yaml_signal_headers_value(yaml_cfg, "traces"),
        headers,
    )

    metrics_exporter = _normalize(
        signal_value("metrics", "exporter", "OTEL_METRICS_EXPORTER", exporter, exporter), exporter
    )
    metrics_endpoint = signal_value(
        "metrics", "endpoint", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", endpoint, endpoint
    )
    metrics_protocol = _normalize(
        signal_value(
            "metrics", "protocol", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", protocol, protocol
        ),
        protocol,
    )
    metrics_headers = _headers_value(
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        _yaml_signal_headers_value(yaml_cfg, "metrics"),
        headers,
    )


    logs_exporter = _normalize(
        signal_value("logs", "exporter", "OTEL_LOGS_EXPORTER", exporter, exporter), exporter
    )
    logs_endpoint = signal_value(
        "logs", "endpoint", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint, endpoint
    )
    logs_protocol = _normalize(
        signal_value("logs", "protocol", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", protocol, protocol),
        protocol,
    )
    logs_headers = _headers_value(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        _yaml_signal_headers_value(yaml_cfg, "logs"),
        headers,
    )

    sample_rate = _coerce_value("OTEL_SAMPLE_RATE", yaml_cfg.get("sample_rate"), 1.0, _parse_float)
    max_attributes = _coerce_value(
        "OTEL_MAX_ATTRIBUTES", yaml_cfg.get("max_attributes"), 128, _parse_int
    )
    attribute_value_max_length = _coerce_value(
        "OTEL_ATTRIBUTE_VALUE_MAX_LENGTH",
        yaml_cfg.get("attribute_value_max_length"),
        10240,
        _parse_int,
    )
    threshold_yaml = session_cfg.get(
        "stuck_threshold_ms", yaml_cfg.get("session_stuck_threshold_ms")
    )
    interval_yaml = session_cfg.get(
        "stuck_check_interval_s", yaml_cfg.get("session_stuck_check_interval_s")
    )

    claw_id = _string_value("OTEL_CLAW_ID", yaml_cfg.get("claw_id"), "") or None
    return TelemetryConfig(
        enabled=_coerce_value("OTEL_ENABLED", yaml_cfg.get("enabled"), False, _parse_bool),
        exporter=exporter,
        endpoint=endpoint,
        protocol=protocol,
        headers=headers,
        traces_exporter=traces_exporter,
        traces_endpoint=traces_endpoint,
        traces_protocol=traces_protocol,
        traces_headers=traces_headers,
        metrics_exporter=metrics_exporter,
        metrics_endpoint=metrics_endpoint,
        metrics_protocol=metrics_protocol,
        metrics_headers=metrics_headers,
        logs_exporter=logs_exporter,
        logs_endpoint=logs_endpoint,
        logs_protocol=logs_protocol,
        logs_headers=logs_headers,
        service_name=_string_value(
            "OTEL_SERVICE_NAME", yaml_cfg.get("service_name"), "jiuwenclaw"
        ),
        sample_rate=max(0.0, min(1.0, sample_rate)),
        max_attributes=max(1, max_attributes),
        attribute_value_max_length=max(1, attribute_value_max_length),
        redact_prompts=_coerce_value(
            "OTEL_REDACT_PROMPTS", yaml_cfg.get("redact_prompts"), False, _parse_bool
        ),
        redact_completions=_coerce_value(
            "OTEL_REDACT_COMPLETIONS", yaml_cfg.get("redact_completions"), False, _parse_bool
        ),
        log_messages=_coerce_value(
            "OTEL_LOG_MESSAGES", yaml_cfg.get("log_messages"), True, _parse_bool
        ),
        claw_id=claw_id,
        session_stuck_threshold_ms=max(
            0.0,
            _coerce_value(
                "OTEL_SESSION_STUCK_THRESHOLD_MS", threshold_yaml, 300000.0, _parse_float
            ),
        ),
        session_stuck_check_interval_s=max(
            0.0,
            _coerce_value(
                "OTEL_SESSION_STUCK_CHECK_INTERVAL_S", interval_yaml, 30.0, _parse_float
            ),
        ),
    )
