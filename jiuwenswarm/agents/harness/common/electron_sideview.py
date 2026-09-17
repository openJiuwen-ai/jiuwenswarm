# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in Electron binding: independent pages, shared persistent login state.

Resolve targets when the MCP subprocess starts, not when an agent spec is
built. A spec may outlive an evicted page or an Electron restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.harness.tools.browser_move.playwright_runtime.config import RuntimeSettings

# 发现文件由 Electron 每 10s 心跳刷新；超过该窗口视为陈旧（崩溃残留）。
DISCOVERY_FILE_NAME = "electron-browser-endpoints.json"
DISCOVERY_MAX_AGE_S = 30.0
# Set in the launcher environment to keep the external managed browser.
FORCE_MANAGED_ENV = "JIUWENSWARM_BROWSER_FORCE_MANAGED"
DISCOVERY_OPT_IN_ENV = "JIUWENSWARM_ELECTRON_BROWSER"
_discovery_lock = threading.RLock()
_discovery_original: dict[str, str | None] = {}
_discovery_applied: dict[str, str] = {}


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _restore_discovery_env() -> None:
    # Do not overwrite configuration changed by the user since injection.
    for key, injected in _discovery_applied.items():
        if os.environ.get(key) != injected:
            continue
        original = _discovery_original.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
    _discovery_applied.clear()
    _discovery_original.clear()


def electron_target_resolver_base() -> str:
    """Refresh owned discovery values before returning a resolver URL."""
    apply_electron_discovery_browser_env()
    if _enabled(FORCE_MANAGED_ENV) or os.getenv("BROWSER_DRIVER", "").strip().lower() not in {"", "remote"}:
        return ""
    env_base = (os.getenv("PLAYWRIGHT_MCP_TARGET_RESOLVER") or "").strip().rstrip("/")
    return env_base


def electron_browser_selected() -> bool:
    """Whether an explicit Electron runtime currently owns browser launch."""
    resolver = electron_target_resolver_base()
    if _enabled(FORCE_MANAGED_ENV) or os.getenv("BROWSER_DRIVER", "").strip().lower() not in {"", "remote"}:
        return False
    return bool(resolver or (os.getenv("PLAYWRIGHT_MCP_TARGET_ID") or "").strip())


def electron_discovery_file_path() -> Path:
    """Return the discovery file path inside the shared jiuwenswarm data dir."""
    data_dir = (os.getenv("JIUWENSWARM_DATA_DIR") or "").strip() or str(Path.home() / ".jiuwenswarm")
    return Path(data_dir) / "runtime_state" / DISCOVERY_FILE_NAME


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check (never raises)."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == still_active
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def load_electron_browser_endpoints(
    *,
    max_age_s: float = DISCOVERY_MAX_AGE_S,
) -> dict[str, Any] | None:
    """Return the fresh Electron browser endpoints payload, or None.

    跳过条件（返回 None）：
    - 本进程由 Electron 亲自 spawn（env 已是权威，无需发现）
    - 逃生开关 JIUWENSWARM_BROWSER_FORCE_MANAGED=1
    - 文件缺失/损坏、心跳超时、pid 已退出
    """
    if (os.getenv("JIUWENSWARM_ELECTRON") or "").strip() == "1":
        return None
    if not _enabled(DISCOVERY_OPT_IN_ENV) or _enabled(FORCE_MANAGED_ENV):
        return None
    if os.getenv("BROWSER_DRIVER", "").strip().lower() not in {"", "remote"}:
        return None
    try:
        # utf-8-sig：容忍编辑器/工具链写入的 BOM 头。
        raw = electron_discovery_file_path().read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    cdp_endpoint = str(payload.get("cdp_endpoint") or "").strip()
    target_resolver = str(payload.get("target_resolver") or "").strip()
    written_at = payload.get("written_at")
    pid = payload.get("pid")
    if not cdp_endpoint or not target_resolver:
        return None
    try:
        age_s = abs(time.time() * 1000 - float(written_at)) / 1000.0
    except (TypeError, ValueError):
        return None
    if age_s > max_age_s:
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid_int):
        return None
    return payload


def apply_electron_discovery_browser_env() -> bool:
    """Apply explicit discovery, restoring only our own overrides when stale."""
    with _discovery_lock:
        # Spawned and explicitly configured remote runtimes are authoritative.
        explicit_resolver = os.environ.get("PLAYWRIGHT_MCP_TARGET_RESOLVER")
        if explicit_resolver and explicit_resolver != _discovery_applied.get("PLAYWRIGHT_MCP_TARGET_RESOLVER"):
            _restore_discovery_env()
            return False
        if _apply_discovered_env():
            return True
        _restore_discovery_env()
        return False


def _apply_discovered_env() -> bool:
    endpoints = load_electron_browser_endpoints()
    if endpoints is None:
        return False
    cdp_endpoint = str(endpoints.get("cdp_endpoint") or "").strip()
    target_resolver = str(endpoints.get("target_resolver") or "").strip()
    if not cdp_endpoint or not target_resolver:
        return False

    command = str(endpoints.get("mcp_command") or "").strip()
    raw_args = endpoints.get("mcp_args")
    if not command or not isinstance(raw_args, list) or not raw_args:
        return False
    raw_env = endpoints.get("env_json")
    if not isinstance(raw_env, dict):
        return False
    merged = {str(key): str(value) for key, value in raw_env.items()}
    merged.update(PLAYWRIGHT_MCP_CDP_ENDPOINT=cdp_endpoint, PLAYWRIGHT_MCP_TARGET_RESOLVER=target_resolver)
    values = {
        "BROWSER_DRIVER": "remote",
        "BROWSER_SHARED_CONTROL": "1",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT": cdp_endpoint,
        "PLAYWRIGHT_MCP_TARGET_RESOLVER": target_resolver,
        "PLAYWRIGHT_MCP_COMMAND": command,
        "PLAYWRIGHT_MCP_ARGS": json.dumps([str(arg) for arg in raw_args]),
        "PLAYWRIGHT_MCP_ENV_JSON": json.dumps(merged),
    }
    for key, value in values.items():
        _discovery_original.setdefault(key, os.environ.get(key))
        _discovery_applied[key] = value
        os.environ[key] = value
    return True


def apply_session_sideview_target(
    settings: "RuntimeSettings", session_id: str, *, member_id: str = "", label: str = ""
) -> "RuntimeSettings":
    """Bind MCP identity to a panel; its wrapper resolves and leases the live page.

    A separate server_id is essential even for single-agent conversations:
    the SDK otherwise reuses the first registered MCP process across sessions.
    """
    resolver = electron_target_resolver_base()
    session_id = session_id.strip()
    if not resolver or not session_id:
        return settings
    mcp_cfg = settings.mcp_cfg
    params: dict[str, Any] = dict(getattr(mcp_cfg, "params", {}) or {})
    env_map: dict[str, str] = dict(params.get("env") or {})
    env_map.pop("PLAYWRIGHT_MCP_TARGET_ID", None)
    member_id = member_id.strip()
    env_map.update({
        "PLAYWRIGHT_MCP_TARGET_RESOLVER": resolver,
        "PLAYWRIGHT_MCP_SESSION_ID": session_id,
        "PLAYWRIGHT_MCP_MEMBER_ID": member_id,
        "PLAYWRIGHT_MCP_PANEL_LABEL": label.strip() or member_id,
    })
    params["env"] = env_map
    identity = json.dumps([session_id, member_id], ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    new_cfg = mcp_cfg.model_copy(update={
        "params": params,
        "server_id": f"playwright_electron_{key}",
        # Registry ownership is by server_id. Preserve the SDK model-facing
        # namespace and permission classification, without overlong tool names.
        "server_name": "playwright-official",
    })
    return replace(settings, mcp_cfg=new_cfg)


__all__ = [
    "apply_electron_discovery_browser_env",
    "apply_session_sideview_target",
    "electron_discovery_file_path",
    "electron_browser_selected",
    "electron_target_resolver_base",
    "load_electron_browser_endpoints",
]
