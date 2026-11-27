# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Telemetry configuration — environment variables take precedence over config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _normalize_exporter(value: str, default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or default


def _normalize_protocol(value: str, default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or default


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    exporter: str = "none"          # otlp / console / none
    endpoint: str = "http://localhost:4317"
    protocol: str = "grpc"          # grpc / http
    headers: dict[str, str] = field(default_factory=dict)
    traces_exporter: str = "none"   # otlp / console / none
    traces_endpoint: str = "http://localhost:4317"
    traces_protocol: str = "grpc"   # grpc / http
    traces_headers: dict[str, str] = field(default_factory=dict)
    metrics_exporter: str = "none"  # otlp / console / none
    metrics_endpoint: str = "http://localhost:4317"
    metrics_protocol: str = "grpc"  # grpc / http
    metrics_headers: dict[str, str] = field(default_factory=dict)
    log_messages: bool = True       # record full message content in span events
    service_name: str = "jiuwenclaw"
    claw_id: str | None = None
    session_stuck_threshold_ms: float = 300000.0     # 5 min
    session_stuck_check_interval_s: float = 30.0     # check every 30s


def _str_env(key: str, default: str) -> str:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip() or default


def _bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes")


def _coerce_float(value, fallback: float) -> float:
    candidate = value.strip() if isinstance(value, str) else value
    try:
        return float(candidate)
    except (ValueError, TypeError):
        return fallback


def _float_env(key: str, default, fallback: float) -> float:
    default_float = _coerce_float(default, fallback)
    env_val = os.getenv(key)
    if env_val is None:
        return default_float
    return _coerce_float(env_val, default_float)


def _optional_str_env(key: str, default: str | None = None) -> str | None:
    val = os.getenv(key)
    if val is None:
        val = default
    if val is None:
        return None
    normalized = str(val).strip()
    return normalized or None


def _parse_headers(value) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in value.items()
            if str(k).strip()
        }
    if isinstance(value, str):
        headers: dict[str, str] = {}
        for item in value.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, raw_val = item.split("=", 1)
            key = key.strip()
            raw_val = raw_val.strip()
            if key:
                headers[key] = raw_val
        return headers
    return {}


def _headers_env(key: str, default) -> dict[str, str]:
    if key in os.environ:
        return _parse_headers(os.getenv(key, ""))
    return _parse_headers(default)


def _yaml_signal_cfg(yaml_cfg: dict, signal: str) -> dict:
    cfg = yaml_cfg.get(signal, {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def _yaml_signal_value(yaml_cfg: dict, signal: str, key: str):
    flat_key = f"{signal}_{key}"
    if flat_key in yaml_cfg:
        return yaml_cfg.get(flat_key)
    return _yaml_signal_cfg(yaml_cfg, signal).get(key)


def load_telemetry_config() -> TelemetryConfig:
    """Load telemetry config from env vars, falling back to config.yaml."""
    # Try config.yaml first
    yaml_cfg: dict = {}
    try:
        from jiuwenclaw.config import get_config
        yaml_cfg = get_config().get("telemetry", {}) or {}
    except Exception:
        pass

    session_cfg = yaml_cfg.get("session", {}) or {}
    common_exporter = _normalize_exporter(
        _str_env("OTEL_EXPORTER_TYPE", str(yaml_cfg.get("exporter", "none"))),
        "none",
    )
    common_endpoint = _str_env(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        str(yaml_cfg.get("endpoint", "http://localhost:4317")),
    )
    common_protocol = _normalize_protocol(
        _str_env("OTEL_EXPORTER_OTLP_PROTOCOL", str(yaml_cfg.get("protocol", "grpc"))),
        "grpc",
    )
    common_headers = _headers_env(
        "OTEL_EXPORTER_OTLP_HEADERS",
        yaml_cfg.get("headers", {}),
    )
    traces_exporter = _normalize_exporter(
        _str_env(
            "OTEL_TRACES_EXPORTER",
            str(_yaml_signal_value(yaml_cfg, "traces", "exporter") or common_exporter),
        ),
        common_exporter,
    )
    traces_endpoint = _str_env(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        str(_yaml_signal_value(yaml_cfg, "traces", "endpoint") or common_endpoint),
    )
    traces_protocol = _normalize_protocol(
        _str_env(
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
            str(_yaml_signal_value(yaml_cfg, "traces", "protocol") or common_protocol),
        ),
        common_protocol,
    )
    traces_headers = _headers_env(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        _yaml_signal_value(yaml_cfg, "traces", "headers") or common_headers,
    )
    metrics_exporter = _normalize_exporter(
        _str_env(
            "OTEL_METRICS_EXPORTER",
            str(_yaml_signal_value(yaml_cfg, "metrics", "exporter") or common_exporter),
        ),
        common_exporter,
    )
    metrics_endpoint = _str_env(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        str(_yaml_signal_value(yaml_cfg, "metrics", "endpoint") or common_endpoint),
    )
    metrics_protocol = _normalize_protocol(
        _str_env(
            "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
            str(_yaml_signal_value(yaml_cfg, "metrics", "protocol") or common_protocol),
        ),
        common_protocol,
    )
    metrics_headers = _headers_env(
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        _yaml_signal_value(yaml_cfg, "metrics", "headers") or common_headers,
    )

    return TelemetryConfig(
        enabled=_bool_env("OTEL_ENABLED", yaml_cfg.get("enabled", False)),
        exporter=common_exporter,
        endpoint=common_endpoint,
        protocol=common_protocol,
        headers=common_headers,
        traces_exporter=traces_exporter,
        traces_endpoint=traces_endpoint,
        traces_protocol=traces_protocol,
        traces_headers=traces_headers,
        metrics_exporter=metrics_exporter,
        metrics_endpoint=metrics_endpoint,
        metrics_protocol=metrics_protocol,
        metrics_headers=metrics_headers,
        log_messages=_bool_env("OTEL_LOG_MESSAGES", yaml_cfg.get("log_messages", True)),
        service_name=os.getenv(
            "OTEL_SERVICE_NAME",
            yaml_cfg.get("service_name", "jiuwenclaw"),
        ).strip(),
        claw_id=_optional_str_env(
            "OTEL_CLAW_ID",
            yaml_cfg.get("claw_id"),
        ),
        session_stuck_threshold_ms=_float_env(
            "OTEL_SESSION_STUCK_THRESHOLD_MS",
            session_cfg.get("stuck_threshold_ms", 300000.0),
            300000.0,
        ),
        session_stuck_check_interval_s=_float_env(
            "OTEL_SESSION_STUCK_CHECK_INTERVAL_S",
            session_cfg.get("stuck_check_interval_s", 30.0),
            30.0,
        ),
    )
