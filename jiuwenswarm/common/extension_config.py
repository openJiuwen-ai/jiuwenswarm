# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared extension install configuration for every local runtime.

Gateway and AgentServer read the same install root, the same report path and
the same timing values, so both processes observe one installation truth.
Gateway code imports this module only; it never imports
``jiuwenswarm.extensions_v2``.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_DIR = "extension_pkgs"
DEFAULT_STORE_DIR = "extension/store"
DEFAULT_STATE_DIR = "extension/state"
DEFAULT_DATA_DIR = "extension"
DEFAULT_EXAMPLES_DIR = "examples"
INSTALL_DIR_ENV = "EXTENSION_INSTALL_DIR"
STORE_DIR_ENV = "EXTENSION_STORE_DIR"
STATE_DIR_ENV = "EXTENSION_STATE_DIR"
DATA_DIR_ENV = "EXTENSION_DATA_DIR"
INSTALL_REPORT_FILENAME = "extension_install_report.yaml"


@dataclass(frozen=True, slots=True)
class InstallReportSettings:
    """Report timing values, converted from product configuration to seconds."""

    poll_interval: float = 0.5
    dependency_timeout: float = 300.0
    lock_timeout: float = 5.0


def resolve_install_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.install_dir`` to an absolute directory.

    Relative paths are anchored at the user workspace, matching the rest of
    JiuwenSwarm. The environment variable is only a fallback when the config
    has no explicit value.
    """

    return _resolve_configured_dir(
        config,
        key="install_dir",
        env_name=INSTALL_DIR_ENV,
        default=DEFAULT_INSTALL_DIR,
    )


def resolve_store_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.store_dir`` to an absolute directory."""
    return _resolve_configured_dir(
        config,
        key="store_dir",
        env_name=STORE_DIR_ENV,
        default=DEFAULT_STORE_DIR,
    )


def resolve_state_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.state_dir`` to an absolute directory."""
    return _resolve_configured_dir(
        config,
        key="state_dir",
        env_name=STATE_DIR_ENV,
        default=DEFAULT_STATE_DIR,
    )


def resolve_extension_data_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.data_dir`` to an absolute directory."""
    return _resolve_configured_dir(
        config,
        key="data_dir",
        env_name=DATA_DIR_ENV,
        default=DEFAULT_DATA_DIR,
    )


def resolve_install_report_path(config: Mapping[str, Any] | None = None) -> Path:
    """Return the shared installation report path used by every runtime."""
    return resolve_extension_data_dir(config) / INSTALL_REPORT_FILENAME


def resolve_install_report_settings(config: Mapping[str, Any] | None = None) -> InstallReportSettings:
    """Read and validate ``extensions.install_report`` timing values."""
    source = config if config is not None else _read_config()
    extensions = source.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise ValueError("extensions must be a mapping")
    report = extensions.get("install_report", {})
    if not isinstance(report, Mapping):
        raise ValueError("extensions.install_report must be a mapping")

    def number(key: str, default: float) -> float:
        value = report.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"extensions.install_report.{key} must be a positive finite number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"extensions.install_report.{key} must be a positive finite number")
        return float(value)

    return InstallReportSettings(
        number("poll_interval_ms", 500) / 1000,
        number("dependency_timeout_seconds", 300),
        number("lock_timeout_seconds", 5),
    )


def _resolve_configured_dir(
    config: Mapping[str, Any] | None,
    *,
    key: str,
    env_name: str,
    default: str,
) -> Path:
    source = config if config is not None else _read_config()
    raw = _configured_value(source, key)
    if not raw:
        raw = os.getenv(env_name, "").strip() or default

    path = Path(raw).expanduser()
    if not path.is_absolute():
        from jiuwenswarm.common.utils import get_user_workspace_dir

        path = get_user_workspace_dir() / path
    return path.resolve()


def _read_config() -> Mapping[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        value = get_config()
    except Exception:  # noqa: BLE001 - a broken config only disables the feature
        logger.warning(
            "[Extensions] cannot read the configuration; falling back to environment and default directories",
            exc_info=True,
        )
        return {}
    return value if isinstance(value, Mapping) else {}


def _configured_value(config: Mapping[str, Any], key: str) -> str:
    extensions = config.get("extensions")
    if not isinstance(extensions, Mapping):
        return ""
    value = extensions.get(key)
    return value.strip() if isinstance(value, str) else ""
