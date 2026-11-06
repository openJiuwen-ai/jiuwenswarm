# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the extension v2 install store."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_INSTALL_DIR = "extension_pkgs"
DEFAULT_STORE_DIR = "extension/store"
DEFAULT_STATE_DIR = "extension/state"
DEFAULT_EXAMPLES_DIR = "examples"
INSTALL_DIR_ENV = "EXTENSION_INSTALL_DIR"
STORE_DIR_ENV = "EXTENSION_STORE_DIR"
STATE_DIR_ENV = "EXTENSION_STATE_DIR"


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


def resolve_examples_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve the bundled example packages, optionally overridden by config."""

    source = config if config is not None else _read_config()
    raw = _configured_value(source, "examples_dir")
    path = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / DEFAULT_EXAMPLES_DIR
    if not path.is_absolute():
        from jiuwenswarm.common.utils import get_user_workspace_dir

        path = get_user_workspace_dir() / path
    return path.resolve()


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
    except Exception:
        return {}
    return value if isinstance((value := get_config()), Mapping) else {}


def _configured_value(config: Mapping[str, Any], key: str) -> str:
    extensions = config.get("extensions")
    if not isinstance(extensions, Mapping):
        return ""
    value = extensions.get(key)
    return value.strip() if isinstance(value, str) else ""


INSTALL_REPORT_FILENAME = "extension_install_report.yaml"


@dataclass(frozen=True, slots=True)
class InstallReportSettings:
    """Report timing values, converted from product configuration to seconds."""

    poll_interval: float = 0.5
    dependency_timeout: float = 300.0
    lock_timeout: float = 5.0


def resolve_extension_data_dir(config: Mapping[str, Any] | None = None) -> Path:
    return _resolve_configured_dir(config, key="data_dir", env_name="EXTENSION_DATA_DIR", default="extension")


def resolve_install_report_path(config: Mapping[str, Any] | None = None) -> Path:
    return resolve_extension_data_dir(config) / INSTALL_REPORT_FILENAME


def resolve_install_report_settings(config: Mapping[str, Any] | None = None) -> InstallReportSettings:
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

    return InstallReportSettings(number("poll_interval_ms", 500) / 1000,
                                 number("dependency_timeout_seconds", 300), number("lock_timeout_seconds", 5))
