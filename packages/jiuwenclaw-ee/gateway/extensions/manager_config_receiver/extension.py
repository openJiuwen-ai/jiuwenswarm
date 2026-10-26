# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway manager_config_receiver 扩展。"""

from __future__ import annotations

import logging

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.base import BaseExtension
from jiuwenswarm.extensions.types import ExtensionConfig

from .core.instance import InstanceService
from .http.server import ConfigReceiverServer
from .infrastructure.config import get_settings

logger = logging.getLogger(__name__)


class ManagerConfigReceiverExtension(BaseExtension):
    """Gateway HTTP Config Receiver：承接 Claw Manager 配置同步 REST 调用。"""

    def __init__(
        self,
        server: ConfigReceiverServer,
        instance_service: InstanceService | None = None,
    ) -> None:
        self._server = server
        self._instance_service = instance_service or InstanceService()

    async def initialize(self, config: ExtensionConfig) -> None:
        cfg = get_settings()
        if not cfg.gateway_config_receiver_enabled:
            logger.info("[ManagerConfigReceiver] disabled by config")
            return

        await self._server.start()
        await self._instance_service.start()

    async def shutdown(self) -> None:
        await self._instance_service.stop()
        await self._server.stop()


async def register_extensions(registry: ExtensionRegistry) -> list[ManagerConfigReceiverExtension]:
    server = ConfigReceiverServer()
    ext = ManagerConfigReceiverExtension(server)
    await ext.initialize(registry.config)
    return [ext]
