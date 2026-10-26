# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Performance summary configuration — loaded once at process startup."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PerfSummaryConfig:
    enabled: bool = True
    bottleneck_top_n: int = 3
    include_tasks: bool = True
    include_errors: bool = False
    include_by_agent: bool = False


_CONFIG: PerfSummaryConfig | None = None


def _bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes")


def _int_env(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        parsed = int(val.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def load_perf_summary_config() -> PerfSummaryConfig:
    """Load perf.summary config from env vars, falling back to config.yaml."""
    yaml_cfg: dict = {}
    try:
        from jiuwenswarm.common.config import get_config

        yaml_cfg = get_config().get("perf", {}) or {}
    except Exception:
        yaml_cfg = {}

    summary_cfg = yaml_cfg.get("summary", {}) if isinstance(yaml_cfg, dict) else {}
    if not isinstance(summary_cfg, dict):
        summary_cfg = {}

    # Default on when yaml/env omit the key; env PERF_SUMMARY_ENABLED wins when set.
    enabled_default = bool(summary_cfg.get("enabled", True))
    top_n_raw = summary_cfg.get("bottleneck_top_n")
    top_n_default = int(top_n_raw) if top_n_raw is not None else 3
    include_tasks_default = bool(summary_cfg.get("include_tasks", True))
    include_errors_default = bool(summary_cfg.get("include_errors", False))
    include_by_agent_default = bool(summary_cfg.get("include_by_agent", False))

    return PerfSummaryConfig(
        enabled=_bool_env("PERF_SUMMARY_ENABLED", enabled_default),
        bottleneck_top_n=_int_env("PERF_SUMMARY_TOP_N", top_n_default),
        include_tasks=_bool_env("PERF_SUMMARY_INCLUDE_TASKS", include_tasks_default),
        include_errors=_bool_env("PERF_SUMMARY_INCLUDE_ERRORS", include_errors_default),
        include_by_agent=_bool_env(
            "PERF_SUMMARY_INCLUDE_BY_AGENT", include_by_agent_default
        ),
    )


def init_perf_summary_config() -> PerfSummaryConfig:
    """Initialize the startup config snapshot."""
    global _CONFIG
    _CONFIG = load_perf_summary_config()
    return _CONFIG


def get_perf_summary_config() -> PerfSummaryConfig:
    """Return the startup snapshot, loading defaults if init was skipped."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_perf_summary_config()
    return _CONFIG
