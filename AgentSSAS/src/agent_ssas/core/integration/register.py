# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# agent_ssas/core/integration/register.py
"""AgentSSASCore 集成注册模块。

提供与 jiuwenswarm 集成的工厂函数,根据配置模式创建适配后的 SSAS 后端实例。

AgentSSASSecurityRail 在 agent_ssas/backend_client/openjiuwen/ 中实现,
依赖 openjiuwen,不在此处实现。
"""

from __future__ import annotations

import logging

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.access_adapter.agent_remote_backend import (
    AgentSSASRemoteBackend,
)
from agent_ssas.core.framework.access_adapter.protocol import AgentSSASBackendProtocol
from agent_ssas.core.framework.config.settings import AgentSSASConfig, AgentSSASMode

logger = logging.getLogger(__name__)


async def create_backend(config: AgentSSASConfig) -> AgentSSASBackendProtocol:
    """创建 SSAS 后端,根据配置模式返回适配后的实例。

    根据 config.mode 选择接入适配插件实现:
    - INPROCESS: 创建 AgentSSASBackend(进程内模式),并触发 initialize()
    - HTTP: 创建 AgentSSASRemoteBackend(HTTP 模式),无需 initialize()

    Args:
        config: AgentSSASCore 子系统配置。

    Returns:
        已初始化的接入适配插件实例。

    Raises:
        ValueError: 配置的 mode 不支持时。
    """
    if config.mode == AgentSSASMode.INPROCESS:
        backend = AgentSSASBackend(config)
        await backend.initialize()
        logger.info("SSAS 后端创建完成: mode=inprocess")
        return backend

    if config.mode == AgentSSASMode.HTTP:
        backend = AgentSSASRemoteBackend(config)
        logger.info("SSAS 后端创建完成: mode=http, endpoint=%s", config.http_endpoint)
        return backend

    raise ValueError(f"Unsupported mode: {config.mode}")
