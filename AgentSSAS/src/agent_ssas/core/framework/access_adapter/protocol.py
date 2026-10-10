# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 接入适配模块的跨仓接口协议。

定义 AgentSSASBackendProtocol,作为 AgentSSASSecurityRail 与 AgentSSAS 之间的
跨仓接口契约。完整定义见《AgentSSAS_01_整体架构设计文档》第四章。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_ssas.core.framework.core_types.assessment import RiskAssessment


@runtime_checkable
class AgentSSASBackendProtocol(Protocol):
    """AgentSSASSecurityRail 与 AgentSSAS 之间的跨仓接口。

    实现该协议的类负责接收 AgentSSASSecurityRail 上报的事件,
    返回聚合后的风险评估结果 RiskAssessment。
    """

    async def report_event(self, raw_event: dict) -> RiskAssessment:
        """上报事件并返回风险评估结果。

        参数 raw_event 为 AgentSSASSecurityRail 上报的原始事件 dict
        (三层结构:common / payload / metadata)。
        返回聚合后的 RiskAssessment,由 AgentSSASSecurityRail 侧
        映射为 SecurityDecision。
        """
        ...
