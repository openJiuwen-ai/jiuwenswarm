# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway Web HTTP listener lifecycle (uvicorn + FastAPI)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WebHttpTransport:
    """Owns uvicorn server state; does not inherit from WebSocket channel."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._http_server: Any = None
        self._http_task: asyncio.Task[Any] | None = None
        self._http_port: int | None = None

    @property
    def port(self) -> int | None:
        """Listen port when running; otherwise None."""
        return self._http_port

    async def start(self, *, host: str | None = None, port: int | None = None) -> bool:
        """Start Gateway Web HTTP (REST + SSE). Returns False on failure (non-fatal)."""
        from jiuwenswarm.gateway.channel_manager.web.web_http_server import (
            resolve_web_http_port,
            start_web_http_server,
        )

        if self._http_task is not None and not self._http_task.done():
            logger.warning("Web HTTP 已在运行 port=%s", self._http_port)
            return True

        config = self._channel.config
        bind_host = host if host is not None else config.host
        bind_port = int(port) if port is not None else config.http_port
        try:
            server, task = await start_web_http_server(
                self._channel,
                host=bind_host,
                port=bind_port,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Web HTTP 启动失败（非致命）host=%s port=%s: %s",
                bind_host,
                bind_port,
                exc,
            )
            self._http_server = None
            self._http_task = None
            self._http_port = None
            return False

        self._http_server = server
        self._http_task = task
        self._http_port = bind_port

        def _on_http_task_done(done_task: asyncio.Task[Any]) -> None:
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is not None:
                logger.error("[WebHTTP] server task exited: %s", exc, exc_info=exc)
            else:
                logger.warning("[WebHTTP] server task exited unexpectedly")
            if self._http_task is done_task:
                self._http_server = None
                self._http_task = None
                self._http_port = None

        task.add_done_callback(_on_http_task_done)
        return True

    async def stop(self) -> None:
        """Stop Gateway Web HTTP and clear in-flight request outbounds."""
        from jiuwenswarm.gateway.channel_manager.web.web_http_server import stop_web_http_server

        server, task = self._http_server, self._http_task
        self._http_server = None
        self._http_task = None
        self._http_port = None
        await self._channel.delivery.clear()
        if server is None and task is None:
            return
        try:
            await stop_web_http_server(server, task)
        except Exception:  # noqa: BLE001
            logger.debug("Web HTTP stop failed", exc_info=True)
