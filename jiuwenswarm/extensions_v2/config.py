# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the extension v2 install store."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_INSTALL_DIR = "extension_pkgs"
INSTALL_DIR_ENV = "EXTENSION_INSTALL_DIR"


def resolve_install_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.install_dir`` to an absolute directory.

    Relative paths are anchored at the user workspace, matching the rest of
    JiuwenSwarm. The environment variable is only a fallback when the config
    has no explicit value.
    """

    source = config if config is not None else _read_config()
    raw = _configured_value(source)
    if not raw:
        raw = os.getenv(INSTALL_DIR_ENV, "").strip() or DEFAULT_INSTALL_DIR

    path = Path(raw).expanduser()
    if not path.is_absolute():
        from jiuwenswarm.common.utils import get_user_workspace_dir

        path = get_user_workspace_dir() / path
    return path.resolve()


def _read_config() -> Mapping[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        value = get_config()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _configured_value(config: Mapping[str, Any]) -> str:
    extensions = config.get("extensions")
    if not isinstance(extensions, Mapping):
        return ""
    value = extensions.get("install_dir")
    return value.strip() if isinstance(value, str) else ""
