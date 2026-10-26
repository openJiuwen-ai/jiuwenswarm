# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared desktop preferences and read-only backend installation checks."""

import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import Any

DEFAULT_CUA_AGENT_ENABLED = False


def is_cua_agent_enabled(subagent_config: Any) -> bool:
    """Resolve null/missing as the release default without changing saved preferences."""
    value = subagent_config.get("enabled") if isinstance(subagent_config, dict) else None
    return DEFAULT_CUA_AGENT_ENABLED if value is None else value is True


def is_cua_backend_available() -> bool:
    """Check installation prerequisites without loading or starting a desktop backend.

    Availability is independent of user preferences and OS permission grants.
    Windows keeps Core's PyAutoGUI/Pillow single-step backend, including in
    desktop installers; its bundled native runtime serves cua_repl. macOS
    installers use the native backend for both routes. Unknown or incomplete
    installs hide the setting without changing its persisted value.
    """
    try:
        if os.getenv("JIUWENSWARM_RUNTIME_PLATFORM", "").strip().lower() == "harmony":
            return False
        if getattr(sys, "frozen", False) and sys.platform in ("darwin", "win32"):
            bundle = getattr(sys, "_MEIPASS", None)
            if not bundle:
                return False
            root = Path(bundle) / "computer_use/_resources/backends"
            if sys.platform == "darwin":
                driver = root / "macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"
                version = platform.mac_ver()[0].split(".")[0]
                return int(version) >= 14 and driver.is_file() and os.access(driver, os.X_OK)
            directory = root / "windows/.build"
            files = (
                "computer-use-driver.exe", "computer-use-capture.exe", "Interop.NativeUIA.dll",
            )
            if not all((directory / name).is_file() for name in files):
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
