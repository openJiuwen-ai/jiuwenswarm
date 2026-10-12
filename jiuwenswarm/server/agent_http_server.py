# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 入口。默认关掉。打开后和 WebSocket 共用同一份工具箱。

这个对象不持有 WebSocket 服务器，也不走它的分发表。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import uvicorn

from jiuwenswarm.server.agent_http_routes import build_http_app
from jiuwenswarm.server.app_services import AppServices

logger = logging.getLogger(__name__)

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8766

_running: AgentHTTPServer | None = None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    return default


def resolve_http_server_settings(
    *,
    env: dict[str, str] | None = None,
    config_section: Any = None,
) -> tuple[bool, str, int]:
    """解析开关、主机和端口。环境变量优先，缺省时 HTTP 是关的。"""
    source = os.environ if env is None else env
    enabled_raw = source.get("AGENT_HTTP_ENABLED")
    if enabled_raw is not None and str(enabled_raw).strip() != "":
        enabled = _as_bool(enabled_raw, False)
    else:
        section = config_section if isinstance(config_section, dict) else {}
        enabled = _as_bool(section.get("enabled"), False)

    host = str(source.get("AGENT_HTTP_HOST") or "").strip()
    if not host and isinstance(config_section, dict):
        host = str(config_section.get("host") or "").strip()
    host = host or DEFAULT_HTTP_HOST

    port_raw = str(source.get("AGENT_HTTP_PORT") or "").strip()
    if not port_raw and isinstance(config_section, dict):
        port_raw = str(config_section.get("port") or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_HTTP_PORT
    except ValueError:
        port = DEFAULT_HTTP_PORT
    return enabled, host, port


class AgentHTTPServer:
    """听一个 HTTP 端口。构造时只收工具箱。"""

    def __init__(
        self,
        app_services: AppServices,
        *,
        host: str = DEFAULT_HTTP_HOST,
        port: int = DEFAULT_HTTP_PORT,
    ) -> None:
        self._app_services = app_services
        self._host = host
        self._port = port
        self._uvicorn: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        app = build_http_app(self._app_services)
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="info")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(), name="agent-http-server")
        while not server.started:
            if task.done():
                error = task.exception()
                raise RuntimeError("HTTP 入口没有听上端口") from error
            await asyncio.sleep(0.01)
        self._uvicorn = server
        self._task = task
        logger.info("[AgentHTTPServer] listening: http://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        server = self._uvicorn
        task = self._task
        self._uvicorn = None
        self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[AgentHTTPServer] stop failed")


def _config_section() -> Any:
    try:
        from jiuwenswarm.common.config import get_config

        section = get_config().get("http_server")
    except Exception:
        logger.debug("[AgentHTTPServer] 读取 http_server 配置失败", exc_info=True)
        return {}
    return section if isinstance(section, dict) else {}


async def start_http_if_enabled(app_services: AppServices) -> None:
    """配置或环境变量打开时才听端口。关着时什么都不做。"""
    global _running
    enabled, host, port = resolve_http_server_settings(config_section=_config_section())
    if not enabled:
        logger.info("[AgentHTTPServer] disabled")
        return
    if _running is not None:
        return
    server = AgentHTTPServer(app_services, host=host, port=port)
    await server.start()
    _running = server


async def stop_http_server() -> None:
    """进程退出时关掉 HTTP 端口。没开过就直接返回。"""
    global _running
    server = _running
    _running = None
    if server is not None:
        await server.stop()
