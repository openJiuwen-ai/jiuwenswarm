# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenClaw OpenTelemetry GenAI telemetry — public API."""

from __future__ import annotations

_initialized = False


def init_telemetry() -> None:
    """Initialize OpenTelemetry tracing and metrics.

    Reads config from env vars / config.yaml. If telemetry is disabled,
    this is a no-op with zero overhead. Initialization failures are logged
    and silently ignored so they never block the main application.
    """
    global _initialized
    if _initialized:
        return

    from jiuwenclaw.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    if not cfg.enabled:
        return

    from jiuwenclaw.utils import logger

    try:
        from jiuwenclaw.telemetry.provider import init_providers
        from jiuwenclaw.telemetry.instrumentors import apply_instrumentors

        logger.info(
            "[Telemetry] Initializing: traces_exporter=%s, metrics_exporter=%s, "
            "traces_endpoint=%s, metrics_endpoint=%s, log_messages=%s",
            cfg.traces_exporter,
            cfg.metrics_exporter,
            cfg.traces_endpoint,
            cfg.metrics_endpoint,
            cfg.log_messages,
        )

        init_providers(cfg)
        apply_instrumentors(
            log_messages=cfg.log_messages,
            session_stuck_threshold_ms=cfg.session_stuck_threshold_ms,
            session_stuck_check_interval_s=cfg.session_stuck_check_interval_s,
        )

        _initialized = True
        logger.info("[Telemetry] Initialization complete")
    except Exception:
        logger.warning("[Telemetry] Initialization failed, continuing without telemetry", exc_info=True)


def is_telemetry_initialized() -> bool:
    """Check if telemetry has been initialized.

    Returns:
        True if telemetry has been initialized, False otherwise.
    """
    return _initialized
