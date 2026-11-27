# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime Management Extension."""

from __future__ import annotations

import logging

from jiuwenclaw.extensions import ExtensionConfig
from jiuwenclaw.extensions.sdk.agent_server_client import AgentServerClientExtension
from .runtime_management_client import RuntimeManagementAgentClient

logger = logging.getLogger(__name__)

class RuntimeManagementExtension(AgentServerClientExtension):
    """Runtime 管理扩展。"""

    def __init__(self, client: RuntimeManagementAgentClient) -> None:
        self._client = client

    async def initialize(self, config: ExtensionConfig) -> None:
        return None

    def get_client(self) -> RuntimeManagementAgentClient:
        return self._client

    async def shutdown(self) -> None:
        try:
            await self._client.disconnect()
        except Exception as exc:
            logger.warning("[RuntimeManagement] shutdown error: %s", exc)


async def register_extensions(registry) -> list[RuntimeManagementExtension]:
    """注册 Runtime Management 扩展。"""
    client = RuntimeManagementAgentClient()
    ext = RuntimeManagementExtension(client)
    registry.register_agent_server_client(ext)
    return [ext]
