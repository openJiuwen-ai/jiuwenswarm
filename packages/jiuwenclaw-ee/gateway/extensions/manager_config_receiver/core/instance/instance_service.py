# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 实例启动辅助（无主动心跳）。

存活由 Manager 周期探活本机 ``/api/health`` 确认。
"""

from __future__ import annotations

import logging

from ...infrastructure.config import Settings, get_settings

logger = logging.getLogger(__name__)


def resolve_public_endpoint(cfg: Settings | None = None) -> str:
    """Manager 回调用的 Gateway Config Receiver Base URL（无尾斜杠）。

    由 ``GATEWAY_CONFIG_PUBLIC_SCHEME``、``GATEWAY_CONFIG_PUBLIC_HOST`` 和
    ``GATEWAY_CONFIG_HTTP_PORT`` 拼接；host 未配置时默认 ``127.0.0.1``。
    """
    cfg = cfg or get_settings()
    port = int(cfg.gateway_config_http_port or 8775)
    host = (cfg.gateway_config_public_host or "").strip() or "127.0.0.1"
    return f"{cfg.gateway_config_public_scheme}://{host}:{port}"


def resolve_manager_http_base(cfg: Settings | None = None) -> str:
    cfg = cfg or get_settings()
    return (cfg.gateway_manager_http_url or "").strip().rstrip("/")


class InstanceService:
    """启动时确保企业库连接可用。"""

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or get_settings()

    async def start(self) -> None:
        try:
            from jiuwenswarm.infrastructure.db.database import Database

            Database.current()
        except Exception:  # noqa: BLE001
            logger.debug("[InstanceService] Database.current failed", exc_info=True)
            return
        logger.info(
            "[InstanceService] endpoint=%s (Manager health-probes this Gateway)",
            resolve_public_endpoint(self._cfg),
        )

    async def stop(self) -> None:
        return
