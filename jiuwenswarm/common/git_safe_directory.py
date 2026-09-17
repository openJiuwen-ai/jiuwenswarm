"""Helpers for reporting Git dubious ownership checks."""
from __future__ import annotations

import subprocess
from pathlib import Path

_DUBIOUS_OWNERSHIP_MARKERS = (
    "detected dubious ownership in repository",
    "dubious ownership",
)


def is_dubious_ownership_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Return True when a git command failed because of safe.directory checks."""
    if result.returncode == 0:
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker in output for marker in _DUBIOUS_OWNERSHIP_MARKERS)


def safe_directory_value(path: str) -> str:
    """Normalize a project path for ``git config safe.directory``."""
    try:
        return Path(path).expanduser().resolve().as_posix()
    except Exception:  # noqa: BLE001
        return str(path)


def safe_directory_hint(path: str) -> str:
    """Return a user-facing command suggestion for Git safe.directory."""
    safe_dir = safe_directory_value(path)
    return f"请在终端执行: git config --global --add safe.directory {safe_dir}"
