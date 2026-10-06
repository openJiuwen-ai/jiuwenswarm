# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved browser settings shared by the main agent and Swarm providers."""

from __future__ import annotations

import sys
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars


def resolve_chrome_path(config: dict[str, Any] | None) -> str:
    """Resolve a string or platform-specific Chrome path from saved config."""
    if not isinstance(config, dict):
        return ""
    browser = config.get("browser", {})
    if not isinstance(browser, dict):
        return ""
    chrome_path = resolve_env_vars(browser).get("chrome_path", "")
    if isinstance(chrome_path, str):
        return chrome_path.strip()
    if not isinstance(chrome_path, dict):
        return ""
    platform_key = {
        "win32": "windows", "cygwin": "windows", "darwin": "macos",
        "linux": "linux", "linux2": "linux",
    }.get(sys.platform, "default")
    for key in (platform_key, "default"):
        value = chrome_path.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
