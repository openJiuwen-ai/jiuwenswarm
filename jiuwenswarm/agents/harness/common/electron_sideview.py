# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Electron sideview target binding for per-session browser isolation.

Electron 主进程为每个会话维护独立 BrowserView（独立 partition），并暴露
target resolver（PLAYWRIGHT_MCP_TARGET_RESOLVER）：GET /<sessionId> 返回该
会话视图的 CDP TargetID。Electron 的静态 spawn env 不再携带全局
PLAYWRIGHT_MCP_TARGET_ID——browser subagent 构建时按会话解析 target 并注入
该会话 MCP 配置的 env，使 @playwright/mcp wrapper 精确绑定本会话视图。

Electron（dev / 完整包 / FrontendOnly）会把随机端口的 CDP endpoint 与
resolver 心跳写入发现文件（runtime_state/electron-browser-endpoints.json）。
未被 Electron 亲自 spawn 的后端（如 FrontendOnly 场景外部启动的服务）在
env 缺失时可经由该文件发现 Electron 壳，获得与 spawn 注入一致的浏览器
绑定，从而让三种运行形态（npm run dev / 仅前端包 / 完整包）效果收敛。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from openjiuwen.harness.tools.browser_move.playwright_runtime.config import RuntimeSettings

logger = logging.getLogger(__name__)

TARGET_RESOLVER_TIMEOUT_S = 15.0

# 发现文件由 Electron 每 10s 心跳刷新；超过该窗口视为陈旧（崩溃残留）。
DISCOVERY_FILE_NAME = "electron-browser-endpoints.json"
DISCOVERY_MAX_AGE_S = 30.0
# 逃生开关：Electron 壳在跑但强制使用本机 managed 浏览器时在 .env 配置为 1。
FORCE_MANAGED_ENV = "JIUWENSWARM_BROWSER_FORCE_MANAGED"


def electron_target_resolver_base() -> str:
    """Return the Electron target resolver base URL, or '' when absent.

    优先级：Electron spawn 注入的 env > 发现文件（新鲜且 pid 存活）> 空。
    """
    env_base = (os.getenv("PLAYWRIGHT_MCP_TARGET_RESOLVER") or "").strip().rstrip("/")
    if env_base:
        return env_base
    endpoints = load_electron_browser_endpoints()
    if endpoints is not None:
        return str(endpoints.get("target_resolver") or "").strip().rstrip("/")
    return ""


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

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
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
    if (os.getenv(FORCE_MANAGED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}:
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
    """Apply discovered Electron browser endpoints into os.environ.

    仅当 env 未经 Electron spawn 注入且发现文件有效时生效；等价于
    Electron spawnService 的浏览器 env 契约（remote driver + CDP endpoint +
    resolver + target wrapper 启动参数）。返回是否实际应用。
    """
    endpoints = load_electron_browser_endpoints()
    if endpoints is None:
        return False
    cdp_endpoint = str(endpoints.get("cdp_endpoint") or "").strip()
    target_resolver = str(endpoints.get("target_resolver") or "").strip()
    if not cdp_endpoint or not target_resolver:
        return False

    os.environ["BROWSER_DRIVER"] = "remote"
    os.environ["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = cdp_endpoint
    os.environ["PLAYWRIGHT_MCP_TARGET_RESOLVER"] = target_resolver

    command = str(endpoints.get("mcp_command") or "").strip()
    raw_args = endpoints.get("mcp_args")
    if command and isinstance(raw_args, list) and raw_args:
        os.environ["PLAYWRIGHT_MCP_COMMAND"] = command
        os.environ["PLAYWRIGHT_MCP_ARGS"] = json.dumps([str(arg) for arg in raw_args])

    raw_env_json = endpoints.get("env_json")
    if isinstance(raw_env_json, dict) and raw_env_json:
        merged = {str(key): str(value) for key, value in raw_env_json.items()}
        merged.setdefault("PLAYWRIGHT_MCP_CDP_ENDPOINT", cdp_endpoint)
        merged.setdefault("PLAYWRIGHT_MCP_TARGET_RESOLVER", target_resolver)
        os.environ["PLAYWRIGHT_MCP_ENV_JSON"] = json.dumps(merged)

    logger.info(
        "[electron.sideview] discovered Electron shell; browser runtime bound: "
        "cdp=%s resolver=%s pid=%s",
        cdp_endpoint,
        target_resolver,
        endpoints.get("pid"),
    )
    return True


def resolve_session_sideview_target(session_id: str) -> str | None:
    """Ask Electron for the session sideview's CDP TargetID.

    失败返回 None（不抛出）：调用方按"无注入"处理，browser subagent 保留
    openjiuwen 默认行为；诊断信息走日志。
    """
    base = electron_target_resolver_base()
    normalized = (session_id or "").strip()
    if not base or not normalized:
        return None
    url = f"{base}/{quote(normalized, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=TARGET_RESOLVER_TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.exception("[electron.sideview] target resolve failed: session=%s", normalized)
        return None
    target_id = str(payload.get("targetId") or "").strip()
    if not target_id:
        logger.warning("[electron.sideview] resolver returned no targetId: session=%s", normalized)
        return None
    return target_id


def apply_session_sideview_target(settings: "RuntimeSettings", session_id: str) -> "RuntimeSettings":
    """Inject the session sideview TargetID into the browser agent's MCP env.

    ``settings.mcp_cfg.params.env`` 里的 ``PLAYWRIGHT_MCP_TARGET_ID`` 决定
    target_mcp_wrapper 绑定的 CDP target。RuntimeSettings 是 frozen dataclass，
    返回替换 mcp_cfg 后的副本；解析失败时原样返回。
    """
    target_id = resolve_session_sideview_target(session_id)
    if target_id is None:
        return settings
    mcp_cfg = settings.mcp_cfg
    params: dict[str, Any] = dict(getattr(mcp_cfg, "params", {}) or {})
    env_map: dict[str, str] = dict(params.get("env") or {})
    env_map["PLAYWRIGHT_MCP_TARGET_ID"] = target_id
    params["env"] = env_map
    new_cfg = mcp_cfg.model_copy(update={"params": params})
    return replace(settings, mcp_cfg=new_cfg)


__all__ = [
    "apply_electron_discovery_browser_env",
    "apply_session_sideview_target",
    "electron_discovery_file_path",
    "electron_target_resolver_base",
    "load_electron_browser_endpoints",
    "resolve_session_sideview_target",
]
