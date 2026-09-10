# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import logging

from uvicorn import Config, Server

from ..infrastructure.config import get_settings
from .app import create_app

logger = logging.getLogger(__name__)


class ConfigReceiverServer:
    """Uvicorn HTTP server for Manager → Gateway config sync."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._host = cfg.gateway_config_http_host
        self._port = int(cfg.gateway_config_http_port)
        self._app = create_app()
        self._server: Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # 嵌套在 Gateway 进程内：关掉 uvicorn 默认 log_config / access_log，
        # 避免 AccessFormatter 与主进程 logging 冲突（expected 5 args, got 0）。
        config = Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="info",
            loop="asyncio",
            log_config=None,
            access_log=False,
            # Proxy headers are handled by create_app() with the configured trust list.
            proxy_headers=False,
        )
        self._server = Server(config)

        async def _serve() -> None:
            await self._server.serve()

        self._task = asyncio.create_task(_serve(), name="manager-config-receiver-http")
        logger.info(
            "[ManagerConfigReceiver] HTTP server starting host=%s port=%s",
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        task = self._task
        if task is not None:
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except asyncio.TimeoutError:
                    task.cancel()
            self._task = None
        self._server = None
        logger.info("[ManagerConfigReceiver] HTTP server stopped")
