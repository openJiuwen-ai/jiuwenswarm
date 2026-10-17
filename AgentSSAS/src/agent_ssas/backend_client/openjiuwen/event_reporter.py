# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EventReporter - 上报模块,上报 raw_event 并映射 RiskAssessment 为 SecurityDecision。"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.harness.rails.security.base_security_rail import (
    SecurityAlert,
    SecurityAlertLevel,
    SecurityAllow,
    SecurityDecision,
    SecurityReject,
)

from agent_ssas.backend_client.openjiuwen.policy_loader import get_alert_level, get_policy
from agent_ssas.core.framework.access_adapter.protocol import AgentSSASBackendProtocol


class EventReporter:
    """上报模块。

    通过 AgentSSASBackendProtocol.report_event(raw_event) 将 raw_event 上报给 AgentSSAS,
    获取返回的 RiskAssessment,再通过 assessment_to_decision() 映射为 SecurityDecision。
    异常时 fail-open 返回 SecurityAllow()。

    决策映射基于独立 YAML 策略文件(decision_policies.yaml),
    策略模式控制 risk_level → SecurityDecision 的映射,
    AlertLevel 映射也由策略文件的 alert_levels 配置加载。
    """

    def __init__(self, backend: AgentSSASBackendProtocol, policy_name: str = "observe_only") -> None:
        self._backend = backend
        self._policy = get_policy(policy_name)

    async def report(self, event_dict: dict[str, Any]) -> SecurityDecision:
        """上报 raw_event 给 AgentSSAS,获取 RiskAssessment 并映射为 SecurityDecision。

        异常时 fail-open 返回 SecurityAllow()。
        """
        try:
            assessment = await self._backend.report_event(event_dict)
            return self.assessment_to_decision(assessment)
        except Exception:
            logger.warning(
                "[AgentSSASSecurityRail] report_event failed, fail-open returning Allow",
                exc_info=True,
            )
            return SecurityAllow()

    async def safe_report(self, event_dict: dict[str, Any]) -> None:
        """安全上报 raw_event(fail-open,异常不阻断)。

        供采集模块(直接覆写钩子的事件)使用,不返回决策。
        """
        try:
            await self._backend.report_event(event_dict)
        except Exception:
            logger.warning(
                "[AgentSSASSecurityRail] report_event failed for event_type=%s, fail-open",
                event_dict.get("common", {}).get("event_type", "unknown"),
                exc_info=True,
            )

    def assessment_to_decision(self, assessment) -> SecurityDecision:
        """基于策略表将 RiskAssessment 映射为 SecurityDecision。

        策略表定义 risk_level → action(reject/alert/allow)的映射。
        AlertLevel 由策略文件的 alert_levels 配置决定(risk_level → alert_level 名称)。
        """
        # 统一 risk_level 为字符串值
        risk_level = assessment.risk_level
        risk_level_str = risk_level.value if hasattr(risk_level, "value") else str(risk_level)

        action = self._policy.get(risk_level_str, "allow")

        if action == "reject":
            return SecurityReject(
                message=f"[AgentSSAS] 阻断: {assessment.risk_type}",
                result={"risk_assessment": assessment.to_dict()},
            )
        elif action == "alert":
            alert_level_name = get_alert_level(risk_level_str)
            alert_level = self._alert_level_from_name(alert_level_name)
            return SecurityAlert(
                message=f"[AgentSSAS] {risk_level_str}级告警: {assessment.risk_type}",
                level=alert_level,
                alert_type=assessment.risk_type or "security",
            )
        return SecurityAllow()

    @staticmethod
    def _alert_level_from_name(name: str) -> SecurityAlertLevel:
        """将 AlertLevel 名称字符串映射为 SecurityAlertLevel 枚举。

        从策略文件 alert_levels 配置加载的名称(error/warning/info)转换为
        agent-core 的 SecurityAlertLevel 枚举。未知值回退为 WARNING。
        """
        mapping = {
            "error": SecurityAlertLevel.ERROR,
            "warning": SecurityAlertLevel.WARNING,
            "info": SecurityAlertLevel.INFO,
            "critical": SecurityAlertLevel.ERROR,
        }
        return mapping.get(name, SecurityAlertLevel.WARNING)
