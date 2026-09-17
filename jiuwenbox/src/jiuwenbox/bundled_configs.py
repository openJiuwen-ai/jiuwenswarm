# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Bundled policy YAML files shipped inside the jiuwenbox package."""

from __future__ import annotations

from pathlib import Path

import jiuwenbox

_CONFIGS_DIR = Path(jiuwenbox.__file__).resolve().parent / "configs"


def configs_dir() -> Path:
    """Directory containing default policy templates bundled with the wheel."""
    return _CONFIGS_DIR


def default_policy_path() -> Path:
    """Default ``default-policy.yaml`` path when ``JIUWENBOX_POLICY_PATH`` is unset."""
    return _CONFIGS_DIR / "default-policy.yaml"
