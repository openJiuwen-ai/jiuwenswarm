# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manager 写库后通知 agent-runtime 强制刷新 AgentServer Pod（与 RuntimeRoutedAgentClient 无关）。

走 config_refresh（优雅日落）：老 Pod 停接新会话、存量会话亲和不受影响，
runtime 重读 DB 存量配置后由 autoscale 重建——区别于 cleanup 批删（强杀，
中断进行中会话，仅灾难恢复/重部署用）。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from jiuwenswarm.common.local_env_config import read_env

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0
_config_update_handle: asyncio.TimerHandle | None = None


def trigger_runtime_config_update() -> None:
    """企业配置变更：防抖后请求 agent-runtime 强制刷新（日落老 Pod + 按新配置重建）。"""
    base = read_env("GATEWAY_RUNTIME_MANAGER_URL", "").strip()
    if not base:
        logger.debug(
            "[ManagerConfigReceiver] GATEWAY_RUNTIME_MANAGER_URL unset, skip runtime notify"
        )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "[ManagerConfigReceiver] no running event loop, runtime config update skipped"
        )
        return

    global _config_update_handle
    if _config_update_handle is not None:
        _config_update_handle.cancel()
    _config_update_handle = loop.call_later(
        _DEBOUNCE_SECONDS,
        _schedule_agentserver_refresh,
    )
    logger.info(
        "[ManagerConfigReceiver] agentserver refresh debounced %.1fs",
        _DEBOUNCE_SECONDS,
    )


def _schedule_agentserver_refresh() -> None:
    global _config_update_handle
    _config_update_handle = None
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _request_agentserver_refresh(),
            name="runtime-agentserver-refresh",
        )
    except RuntimeError:
        logger.warning(
            "[ManagerConfigReceiver] no running event loop, agentserver refresh skipped"
        )


async def _request_agentserver_refresh() -> None:
    base = read_env("GATEWAY_RUNTIME_MANAGER_URL", "").strip().rstrip("/")
    if not base:
        return
    try:
        timeout = float(read_env("GATEWAY_RUNTIME_MANAGER_TIMEOUT", "40"))
    except (TypeError, ValueError):
        timeout = 40.0
    if timeout <= 0:
        timeout = 40.0

    # config_refresh 是无载荷端点（rawdata 非空 → 400），配置由 runtime 自行重读 DB
    url = f"{base}/api/session/config_refresh"
    body: dict[str, Any] = {
        "type": "config_refresh",
        "metadata": {"request_id": f"cfg-{uuid.uuid4().hex[:12]}"},
        "rawdata": {},
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(
                "[ManagerConfigReceiver] agentserver refresh failed status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return
        payload = resp.json()
        raw = payload.get("rawdata") if isinstance(payload.get("rawdata"), dict) else payload
        scopes_refreshed = raw.get("scopes_refreshed") if isinstance(raw, dict) else None
        pods_sunset = raw.get("pods_sunset") if isinstance(raw, dict) else None
        logger.info(
            "[ManagerConfigReceiver] agentserver refresh ok scopes_refreshed=%s pods_sunset=%s",
            scopes_refreshed,
            pods_sunset,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ManagerConfigReceiver] agentserver refresh request failed: %s",
            exc,
        )
