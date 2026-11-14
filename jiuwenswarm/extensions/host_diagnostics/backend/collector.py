"""Read-only runtime diagnostics collection for the host process.

This module intentionally depends on nothing outside the Python standard
library.  That keeps the plugin unit-testable without the full Jiuwen
runtime (no ``openjiuwen``, no ``fastapi``) and guarantees the diagnostics
path can never fail because an optional third-party package is missing.

Every collector is a pure function of its arguments (plus process-wide
read-only facts), so tests can pin the clock with ``now=`` instead of
sleeping.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Module import time is the closest stdlib-only proxy for host process start.
# ``resource.getrusage`` would be more precise but is unavailable on Windows.
_PROCESS_STARTED_AT = time.time()

#: Hard cap on how many environment entries a single call may return.
MAX_ENV_ENTRIES = 64

#: Replacement written in place of a sensitive environment value.
REDACTED = "******"

#: Substrings (matched case-insensitively against the *name*) that mark an
#: environment variable as sensitive.  Values of such variables are never
#: returned, only their names.
_SENSITIVE_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
    "SESSION",
)


def _is_sensitive_env(name: str) -> bool:
    """Return whether an environment variable name looks credential-bearing."""
    upper = name.upper()
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    """Call ``call`` and fall back to ``default`` on any platform quirk."""
    try:
        return call()
    except Exception:  # noqa: BLE001
        return default


def collect_platform() -> dict[str, Any]:
    """Return static operating-system facts."""
    return {
        "system": _safe(platform.system, ""),
        "release": _safe(platform.release, ""),
        "version": _safe(platform.version, ""),
        "machine": _safe(platform.machine, ""),
        "processor": _safe(platform.processor, ""),
        "hostname": _safe(socket.gethostname, ""),
    }


def collect_python() -> dict[str, Any]:
    """Return facts about the interpreter running the host process."""
    return {
        "version": _safe(platform.python_version, ""),
        "implementation": _safe(platform.python_implementation, ""),
        "executable": str(getattr(sys, "executable", "") or ""),
        "prefix": str(getattr(sys, "prefix", "") or ""),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def collect_process(*, now: float | None = None) -> dict[str, Any]:
    """Return facts about the current process.

    Args:
        now: Override for the current epoch seconds (test seam).
    """
    moment = time.time() if now is None else float(now)
    return {
        "pid": os.getpid(),
        "parent_pid": _safe(os.getppid, None),
        "cwd": _safe(os.getcwd, ""),
        "uptime_seconds": round(max(0.0, moment - _PROCESS_STARTED_AT), 3),
        "threads": _safe(threading.active_count, None),
    }


def collect_env(*, limit: int = MAX_ENV_ENTRIES) -> dict[str, Any]:
    """Return a bounded, redacted snapshot of the process environment.

    Only variable *names* of sensitive entries are exposed; their values are
    replaced by :data:`REDACTED`.  The result is sorted by name so callers
    get a stable view.

    Raises:
        ValueError: if ``limit`` is not a positive integer.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")

    names = sorted(os.environ)
    selected = names[:limit]
    entries = {
        name: (REDACTED if _is_sensitive_env(name) else os.environ.get(name, ""))
        for name in selected
    }
    return {
        "entries": entries,
        "total": len(names),
        "returned": len(selected),
        "truncated": len(names) > len(selected),
        "redacted": sum(1 for name in selected if _is_sensitive_env(name)),
    }


def collect_paths(*, extension_dir: str | Path | None = None) -> dict[str, Any]:
    """Return filesystem anchors useful when triaging the host process."""
    return {
        "cwd": _safe(os.getcwd, ""),
        "home": _safe(lambda: str(Path.home()), ""),
        "extension_dir": str(extension_dir) if extension_dir else "",
    }


def collect_summary(
    *,
    now: float | None = None,
    include_env: bool = False,
    env_limit: int = MAX_ENV_ENTRIES,
) -> dict[str, Any]:
    """Aggregate the standard diagnostics payload.

    ``include_env`` is off by default because environment contents are the
    most sensitive part of the snapshot; callers opt in explicitly.
    """
    payload: dict[str, Any] = {
        "platform": collect_platform(),
        "python": collect_python(),
        "process": collect_process(now=now),
    }
    if include_env:
        payload["env"] = collect_env(limit=env_limit)
    return payload


def collect_health(*, now: float | None = None) -> dict[str, Any]:
    """Return a minimal liveness probe payload."""
    moment = time.time() if now is None else float(now)
    return {
        "status": "ok",
        "pid": os.getpid(),
        "uptime_seconds": round(max(0.0, moment - _PROCESS_STARTED_AT), 3),
    }


def describe_plugin(plugin: Any) -> dict[str, Any]:
    """Summarize one duck-typed application plugin.

    Deliberately avoids importing ``jiuwenswarm.extensions`` so a plugin can
    inspect the registry that owns it without an import cycle.
    """
    metadata = _safe(lambda: getattr(plugin, "metadata", None))
    plugin_id = str(
        getattr(plugin, "plugin_id", "") or getattr(metadata, "id", "") or ""
    )

    enabled: bool | None = True
    is_enabled = getattr(plugin, "is_enabled", None)
    if callable(is_enabled):
        enabled = _safe(is_enabled, None)

    permissions = getattr(metadata, "permissions", ()) or ()

    return {
        "plugin_id": plugin_id,
        "name": str(getattr(metadata, "name", "") or plugin_id),
        "version": str(getattr(metadata, "version", "") or ""),
        "description": str(getattr(metadata, "description", "") or ""),
        "package_type": str(getattr(metadata, "package_type", "") or ""),
        "permissions": [str(item) for item in permissions],
        "enabled": enabled,
    }


def describe_application_plugins(registry: Any) -> list[dict[str, Any]]:
    """Return a sorted inventory of application plugins registered on ``registry``."""
    getter = getattr(registry, "get_application_plugins", None)
    if not callable(getter):
        return []
    plugins = _safe(getter, ()) or ()
    described = [describe_plugin(plugin) for plugin in plugins]
    described.sort(key=lambda item: (item["plugin_id"], item["version"]))
    return described


def describe_typed_extensions(registry: Any) -> dict[str, Any]:
    """Report which single-slot extension points are populated.

    Only the concrete class name is exposed -- the point is an operator
    triage signal ("is a southbound client installed?"), not introspection
    of extension internals.
    """

    def _describe(getter_name: str) -> str | None:
        getter = getattr(registry, getter_name, None)
        if not callable(getter):
            return None
        extension = _safe(getter, None)
        return type(extension).__name__ if extension is not None else None

    return {
        "agent_server_client": _describe("get_agent_server_client_extension"),
        "third_agent": _describe("get_third_agent_extension"),
        "crypto_utility": _describe("get_crypto_utility_extension"),
    }
