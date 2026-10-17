# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 接入适配驱动器。

AccessAdapter 是接入适配模块的驱动器,负责根据配置创建并管理
接入适配插件实例(如 AgentSSASBackend)。0.1 版本为简化实现,
主要是 AgentSSASBackend 的薄包装,提供统一的初始化和关闭入口。
"""

from __future__ import annotations

import logging

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment

logger = logging.getLogger(__name__)


class AccessAdapter:
    """接入适配驱动器。

    根据配置创建并持有接入适配插件实例,提供统一的初始化和
    report_event 转发入口。0.1 版本仅支持进程内模式(AgentSSASBackend),
    后续版本可扩展支持 HTTP 远程模式等。

    该类是 AgentSSASBackend 的薄包装,适用于需要统一管理生命周期
    或后续扩展多后端切换的场景。若不需要这层抽象,可直接使用
    AgentSSASBackend。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化接入适配驱动器。

        根据配置创建接入适配插件实例。0.1 版本固定使用
        AgentSSASBackend(进程内模式)。

        Args:
            config: AgentSSAS 子系统配置。
        """
        self._config = config
        # 0.1 版本固定使用进程内模式
        self._backend = AgentSSASBackend(config)

    async def initialize(self) -> None:
        """初始化接入适配插件。

        触发底层接入适配插件的 initialize(),完成检测模块加载等初始化工作。
        """
        await self._backend.initialize()
        logger.info("AccessAdapter 初始化完成")

    async def report_event(self, raw_event: dict) -> RiskAssessment:
        """上报事件并返回风险评估结果。

        将 raw_event 转发给底层接入适配插件处理,返回聚合后的
        RiskAssessment。

        Args:
            raw_event: 原始事件 dict(三层结构:common / payload / metadata)。

        Returns:
            聚合后的 RiskAssessment。
        """
        return await self._backend.report_event(raw_event)

    @property
    def backend(self) -> AgentSSASBackend:
        """底层接入适配插件实例。"""
        return self._backend
