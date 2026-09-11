# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSASSecurityRail 工厂函数。"""

from __future__ import annotations

from openjiuwen.core.common.logging import logger

from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.integration.register import create_backend


async def create_agent_ssas_rail(config: AgentSSASConfig) -> AgentSSASSecurityRail:
    """按 AgentSSASConfig 创建 AgentSSASSecurityRail 实例。

    内部调用 create_backend(config) 创建后端(进程内模式或 HTTP 模式),
    注入到 AgentSSASSecurityRail 构造函数。

    Args:
        config: AgentSSASConfig 配置实例,从 config.yaml 的 ssas 段加载。

    Returns:
        AgentSSASSecurityRail 实例,已注入后端,可直接加入 DeepAgent 的 rails 列表。
    """
    backend = await create_backend(config)
    policy_name = config.decision_policy
    rail = AgentSSASSecurityRail(backend=backend, policy_name=policy_name)
    logger.info(
        "[AgentSSAS] AgentSSASSecurityRail created, priority=%s, mode=%s, policy=%s",
        rail.priority,
        config.mode,
        policy_name,
    )
    return rail
