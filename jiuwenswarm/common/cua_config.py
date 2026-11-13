<<<<<<< HEAD
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
||||||| e487e9a30
=======
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared desktop preferences and read-only backend installation checks."""

import importlib.util
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

from jiuwenswarm.common.runtime_platform import is_harmony_runtime

DEFAULT_CUA_AGENT_ENABLED = False
logger = logging.getLogger(__name__)


def is_cua_agent_enabled(subagent_config: Any) -> bool:
    """Resolve null/missing as the release default without changing saved preferences."""
    value = subagent_config.get("enabled") if isinstance(subagent_config, dict) else None
    return DEFAULT_CUA_AGENT_ENABLED if value is None else value is True


def _is_harmony_desktop() -> bool:
    return (
        os.getenv("JIUWENSWARM_RUNTIME_PLATFORM", "").strip().lower() == "harmony"
        or is_harmony_runtime()
    )


def _installed_repl_unavailable_reason() -> str:
    """Read the optional status probe from the module required for driver preparation."""
    try:
        from computer_use import paths

        check = getattr(paths, "installed_backend_unavailable_reason", None)
        return (check() or "") if check else ""
    except Exception:  # noqa: BLE001 - SDK failures must not prevent settings from loading.
        logger.debug("Failed to inspect the installed Computer Use backend", exc_info=True)
        return "backend_missing"


def cua_repl_unavailable_reason() -> str:
    """Report why the REPL cannot start without preparing the native backend."""
    try:
        if _is_harmony_desktop() or sys.platform not in ("darwin", "win32", "linux"):
            return "unsupported_platform"
        if sys.platform == "darwin" and int(platform.mac_ver()[0].split(".")[0]) < 14:
            return "unsupported_platform"
        if any(importlib.util.find_spec(name) is None for name in ("computer_use", "computer_use_mcp")):
            return "backend_missing"
        if not getattr(sys, "frozen", False):
            if importlib.util.find_spec("computer_use_native_tools") is None:
                return "backend_missing"
            return _installed_repl_unavailable_reason()
        bundle = getattr(sys, "_MEIPASS", None)
        if sys.platform == "linux":
            return "unsupported_platform"
        if not bundle:
            return "backend_missing"
        root = Path(bundle) / "computer_use/_resources/os_adapter"
        if sys.platform == "darwin":
            driver = root / "macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"
            available = driver.is_file() and os.access(driver, os.X_OK)
        else:
            directory = root / "windows/.build"
            files = ("computer-use-driver.exe", "computer-use-capture.exe", "Interop.NativeUIA.dll")
            available = all((directory / name).is_file() for name in files)
        return "" if available else "backend_missing"
    except (ImportError, OSError, ValueError):
        return "backend_missing"


def is_cua_repl_available() -> bool:
    """Check whether the REPL can be registered."""
    return not cua_repl_unavailable_reason()


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


def cua_backend_settings() -> dict[str, str]:
    """Provide the same read-only capability to both settings transports."""
    reason = "" if is_legacy_cua_available() else cua_repl_unavailable_reason()
    return {
        "cua_backend_available": "false" if reason else "true",
        "cua_backend_unavailable_reason": reason,
    }
>>>>>>> origin/develop
