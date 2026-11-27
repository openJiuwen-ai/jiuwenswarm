# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentServer HTTP 启动失败策略（企业版/enforce 致命，个人版可 WS 独活）。

从 ``app_agentserver`` 抽出以便单测不依赖整进程入口的重依赖图。
"""

from __future__ import annotations

from jiuwenswarm.common.utils import logger
from jiuwenswarm.edition import is_enterprise


async def handle_http_startup_exception(
    server,
    exc: BaseException,
    *,
    http_enabled: bool,
    link_mtls_enforced: bool,
) -> None:
    """HTTP 启动抛错时的分流：enforce/企业版致命退出，个人版仅记日志。

    调用方在本函数返回后应将 ``http_server`` 置为 ``None``（个人版 WS 独活）。
    enforce 分支已在上方 ``raise``，不会落到企业版判断（故不再写
    ``or link_mtls_enforced`` 死条件）。
    """
    if link_mtls_enforced:
        await server.stop()
        raise RuntimeError(
            "AgentServer enforce HTTPS startup failed; no WS fallback"
        ) from exc
    if is_enterprise() and http_enabled:
        await server.stop()
        raise RuntimeError(
            "AgentServer enterprise HTTP startup failed; refusing WS-only half-alive"
        ) from exc
    logger.error(
        "[AgentServer] HTTP 入口启动失败，仅 WebSocket 可用: %s", exc, exc_info=True
    )


async def abort_if_http_required_missing(
    server,
    *,
    http_server,
    http_enabled: bool,
    http_host: str,
    http_port: int,
    link_mtls_enforced: bool,
) -> None:
    """HTTP 契约未满足时停 WS 并致命退出（``start()`` 返回 False 的路径）。"""
    if link_mtls_enforced and http_server is None:
        await server.stop()
        raise RuntimeError("AgentServer enforce requires a working HTTPS listener")

    # 企业版 HTTP 已开启却没起来：禁止「WS 独活」。配置口被占时也不再漂移到
    # 未注册端口，交给容器重启自愈，避免 runtime 探针打旧口误杀。
    if is_enterprise() and http_enabled and http_server is None:
        await server.stop()
        raise RuntimeError(
            "AgentServer enterprise requires HTTP on the configured port "
            f"(host={http_host!r} port={http_port}); port drift is disabled"
        )
