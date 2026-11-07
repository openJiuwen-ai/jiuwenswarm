# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程服务。

这一份在进程里启动运行环境、心跳调度器、归档恢复和各类预热。
它不听端口。WebSocket 门和 HTTP 门只负责收发，用的是这里已经启动的服务。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.auth.login_credentials import run_refresh_requests
from jiuwenswarm.common.auth.remote_config import warm_up_in_background

logger = logging.getLogger(__name__)


class ProcessServices:
    """启动进程级服务。调用方传入现有的服务器对象，本类不另造一套运行环境。"""

    _started = False

    def __init__(self, server: Any) -> None:
        self._server = server

    @classmethod
    def started(cls) -> bool:
        """整个进程里的运行环境是否已经拉起来过。WebSocket 和 HTTP 共用这一份。"""
        return cls._started

    @classmethod
    def note_stopped(cls) -> None:
        """进程服务收尾后清掉标记，下次才能再启动。"""
        cls._started = False

    async def start(self) -> None:
        """把运行环境和心跳调度器拉起来。整个进程只启动一次。"""
        if ProcessServices.started():
            logger.warning("[ProcessServices] 进程服务已在运行")
            return

        server = self._server
        await server.activate_configured_kv_cache()
        server.start_archive_recovery()
        server.reset_harness_package_state()
        ProcessServices._started = True
        server.attach_runtime_push_handler()
        logger.info("[ProcessServices] 进程服务已启动")

        server.launch_asset_services()
        server.launch_tokenizer_warmup()
        server.launch_runtime_warmup(self._start_runtime)
        server.launch_mcp_prewarm(self._warmup_mcp_connections)
        server.launch_personal_context()
        warm_up_in_background()
        server.launch_login_credential_refresh(run_refresh_requests)
        await server.bootstrap_internal_jiuwenbox()

    async def _start_runtime(self) -> None:
        """后台启动运行环境，并启动心跳调度器。失败会隔一段时间再试。"""
        server = self._server
        retry_delay = 1.0
        while True:
            try:
                await server.start_agent_runtime()
                await server.start_heartbeat_scheduler()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ProcessServices] Runtime warmup failed; retrying in %.1fs: %s",
                    retry_delay,
                    exc,
                )
                server.notify_runtime_warmup_retry(str(exc))
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 2)
                continue
            server.notify_runtime_ready()
            await server.recover_symphony()
            return

    async def _warmup_mcp_connections(self) -> None:
        try:
            from jiuwenswarm.common.mcp_config import prewarm_connected_mcps

            await prewarm_connected_mcps()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ProcessServices] MCP prewarm failed (will lazy-connect on first chat): %s",
                exc,
            )
