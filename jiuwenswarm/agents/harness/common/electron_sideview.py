# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Electron sideview target binding for per-session browser isolation.

Electron 主进程为每个会话维护独立 BrowserView（独立 partition），并暴露
target resolver（PLAYWRIGHT_MCP_TARGET_RESOLVER）：GET /<sessionId> 返回该
会话视图的 CDP TargetID。Electron 的静态 spawn env 不再携带全局
PLAYWRIGHT_MCP_TARGET_ID——browser subagent 构建时按会话解析 target 并注入
该会话 MCP 配置的 env，使 @playwright/mcp wrapper 精确绑定本会话视图。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from openjiuwen.harness.tools.browser_move.playwright_runtime.config import RuntimeSettings

logger = logging.getLogger(__name__)

TARGET_RESOLVER_TIMEOUT_S = 15.0


def electron_target_resolver_base() -> str:
    """Return the Electron target resolver base URL, or '' when absent."""
    return (os.getenv("PLAYWRIGHT_MCP_TARGET_RESOLVER") or "").strip().rstrip("/")


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
    "apply_session_sideview_target",
    "electron_target_resolver_base",
    "resolve_session_sideview_target",
]
