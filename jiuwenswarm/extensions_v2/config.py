# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the extension v2 install store.

The install root, data root and report timing are shared with the Gateway and
live in :mod:`jiuwenswarm.common.extension_config`; this module re-exports them
so existing callers keep their import path.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jiuwenswarm.common.extension_config import (
    DEFAULT_DATA_DIR,
    DEFAULT_EXAMPLES_DIR,
    DEFAULT_INSTALL_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_STORE_DIR,
    DATA_DIR_ENV,
    INSTALL_DIR_ENV,
    INSTALL_REPORT_FILENAME,
    STATE_DIR_ENV,
    STORE_DIR_ENV,
    InstallReportSettings,
    resolve_extension_data_dir,
    resolve_install_dir,
    resolve_install_report_path,
    resolve_install_report_settings,
    resolve_state_dir,
    resolve_store_dir,
)

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_EXAMPLES_DIR",
    "DEFAULT_INSTALL_DIR",
    "DEFAULT_STATE_DIR",
    "DEFAULT_STORE_DIR",
    "DATA_DIR_ENV",
    "INSTALL_DIR_ENV",
    "INSTALL_REPORT_FILENAME",
    "STATE_DIR_ENV",
    "STORE_DIR_ENV",
    "InstallReportSettings",
    "resolve_examples_dir",
    "resolve_extension_data_dir",
    "resolve_install_dir",
    "resolve_install_report_path",
    "resolve_install_report_settings",
    "resolve_state_dir",
    "resolve_store_dir",
]


def resolve_examples_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve the bundled example packages, optionally overridden by config."""

    source = config if config is not None else _read_config()
    extensions = source.get("extensions")
    raw = extensions.get("examples_dir") if isinstance(extensions, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            from jiuwenswarm.common.utils import get_user_workspace_dir

            path = get_user_workspace_dir() / path
        return path.resolve()
    return (Path(__file__).resolve().parent / DEFAULT_EXAMPLES_DIR).resolve()


def _read_config() -> Mapping[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        value = get_config()
    except Exception:  # noqa: BLE001 - a broken config only disables the feature
        return {}
    return value if isinstance(value, Mapping) else {}
