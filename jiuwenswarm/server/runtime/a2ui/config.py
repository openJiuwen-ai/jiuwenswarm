# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the A2UI feature."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


SUPPORTED_A2UI_PROTOCOL_VERSIONS = frozenset({"0.8"})


@dataclass(frozen=True)
class A2UIConfig:
    """Runtime switches controlling the optional A2UI feature."""

    enabled: bool = False
    protocol_version: str = "0.8"
    stream_validation_enabled: bool = True
    non_web_fallback_enabled: bool = False
    dev_smoke_tools_enabled: bool = False


def _to_bool(value: Any, default: bool) -> bool:
    """Parse config and environment boolean values with a stable default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def get_a2ui_config(config: dict[str, Any] | None = None) -> A2UIConfig:
    """Build A2UI config from a config dictionary plus environment overrides."""
    raw = config or {}
    section = raw.get("a2ui") if isinstance(raw, dict) else {}
    section = section if isinstance(section, dict) else {}

    enabled = _to_bool(section.get("enabled"), False)
    env_enabled = os.getenv("JIUWENSWARM_A2UI_ENABLED")
    if env_enabled is not None:
        enabled = _to_bool(env_enabled, enabled)

    protocol_version = str(
        os.getenv("JIUWENSWARM_A2UI_PROTOCOL_VERSION")
        or section.get("protocol_version")
        or "0.8"
    ).strip()
    if protocol_version not in SUPPORTED_A2UI_PROTOCOL_VERSIONS:
        raise ValueError(f"Unsupported A2UI protocol version: {protocol_version}")

    dev_smoke_tools_enabled = _to_bool(section.get("dev_smoke_tools_enabled"), False)
    env_smoke = os.getenv("JIUWENSWARM_A2UI_SMOKE_TOOLS")
    if env_smoke is not None:
        dev_smoke_tools_enabled = _to_bool(env_smoke, dev_smoke_tools_enabled)

    return A2UIConfig(
        enabled=enabled,
        protocol_version=protocol_version,
        stream_validation_enabled=_to_bool(section.get("stream_validation_enabled"), True),
        non_web_fallback_enabled=_to_bool(section.get("non_web_fallback_enabled"), False),
        dev_smoke_tools_enabled=dev_smoke_tools_enabled,
    )


def get_current_a2ui_config() -> A2UIConfig:
    """Read A2UI config from the active jiuwenswarm runtime config."""
    from jiuwenswarm.common.config import get_config

    return get_a2ui_config(get_config() or {})


def is_a2ui_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether A2UI is enabled for the supplied config."""
    return get_a2ui_config(config).enabled
