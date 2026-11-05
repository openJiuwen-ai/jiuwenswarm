# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the extension v2 install store."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_INSTALL_DIR = "extension_pkgs"
DEFAULT_STORE_DIR = "extension/store"
DEFAULT_STATE_DIR = "extension/state"
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
