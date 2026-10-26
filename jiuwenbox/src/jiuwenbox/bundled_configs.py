# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Bundled policy YAML files shipped inside the jiuwenbox package."""

from __future__ import annotations

import sys
from pathlib import Path

import jiuwenbox

_CONFIGS_DIR = Path(jiuwenbox.__file__).resolve().parent / "configs"


def configs_dir() -> Path:
    """Directory containing default policy templates bundled with the wheel."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for frozen in (
            Path(meipass) / "jiuwenbox_configs" / "configs",
            Path(meipass) / "jiuwenbox" / "configs",
        ):
            if frozen.is_dir():
                return frozen
    return _CONFIGS_DIR


def default_policy_path() -> Path:
    """Default ``default-policy.yaml`` path when ``JIUWENBOX_POLICY_PATH`` is unset."""
    return configs_dir() / "default-policy.yaml"


def base_policy_path() -> Path:
    """Bundled policy path (framework default, read-only, upgraded with the wheel).

    Windows: ``windows-policy.yaml``. Other platforms: ``default-policy.yaml``.
    On Windows, box-server merges this base with the sparse user copy at
    ``JIUWENBOX_POLICY_PATH``. On Linux, ``JIUWENBOX_POLICY_PATH`` replaces
    the base entirely (no merge).
    """
    name = "windows-policy.yaml" if sys.platform == "win32" else "default-policy.yaml"
    return configs_dir() / name
