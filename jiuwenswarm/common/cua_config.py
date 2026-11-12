# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared desktop preferences and read-only backend installation checks."""

import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import Any

from jiuwenswarm.common.runtime_platform import is_harmony_runtime

DEFAULT_CUA_AGENT_ENABLED = False


def is_cua_agent_enabled(subagent_config: Any) -> bool:
    """Resolve null/missing as the release default without changing saved preferences."""
    value = subagent_config.get("enabled") if isinstance(subagent_config, dict) else None
    return DEFAULT_CUA_AGENT_ENABLED if value is None else value is True


def _is_harmony_desktop() -> bool:
    return (
        os.getenv("JIUWENSWARM_RUNTIME_PLATFORM", "").strip().lower() == "harmony"
        or is_harmony_runtime()
    )


def is_cua_repl_available() -> bool:
    """Detect the SDK or bundled driver without preparing the native backend."""
    try:
        if _is_harmony_desktop() or sys.platform not in ("darwin", "win32", "linux"):
            return False
        if sys.platform == "darwin" and int(platform.mac_ver()[0].split(".")[0]) < 14:
            return False
        if any(importlib.util.find_spec(name) is None for name in ("computer_use", "computer_use_mcp")):
            return False
        if not getattr(sys, "frozen", False):
            return importlib.util.find_spec("computer_use_native_tools") is not None
        bundle = getattr(sys, "_MEIPASS", None)
        if not bundle or sys.platform == "linux":
            return False
        root = Path(bundle) / "computer_use/_resources/os_adapter"
        if sys.platform == "darwin":
            driver = root / "macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"
            return driver.is_file() and os.access(driver, os.X_OK)
        directory = root / "windows/.build"
        files = (
            "computer-use-driver.exe", "computer-use-capture.exe", "Interop.NativeUIA.dll",
        )
        return all((directory / name).is_file() for name in files)
    except (ImportError, OSError, ValueError):
        return False


def is_legacy_cua_available() -> bool:
    """Check the prerequisites of agent-core's single-step desktop tools."""
    try:
        if _is_harmony_desktop():
            return False
        modules = ("pyautogui", "PIL")
        if sys.platform.startswith("linux"):
            if not os.getenv("DISPLAY"):
                return False
            modules += ("Xlib",)
        elif sys.platform != "win32":
            return False
        return all(importlib.util.find_spec(name) is not None for name in modules)
    except (ImportError, OSError, ValueError):
        return False


def is_cua_backend_available() -> bool:
    """Report installation capability independently of permissions and preferences."""
    return is_cua_repl_available() or is_legacy_cua_available()
