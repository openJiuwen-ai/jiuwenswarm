# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 安全护栏分析插件。

SecurityRailAnalyzer 是安全护栏检测模块的分析插件,
用于识别并上报所有安全检查类的事件(event_class="security")。
安全护栏已经做了检测,此处只做上报和呈现,不做额外检测。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SecurityRailAnalyzer:
    """安全护栏分析插件。

    识别所有 event_class="security" 的事件,将其风险信息作为
    威胁分析报告输出。安全护栏已经做了检测,此处只做上报和呈现。

    analyze 入口先判断事件类型是否为预期的安全检测事件:
    检查 model_data 中的 event_class 是否等于 "security",
    如果不是则跳过(直接返回无风险报告),避免对生命周期事件误报。
    """

    name: str = "SecurityRailAnalyzer"
    expected_model_type: str = "blank"  # 与 BlankDataModeler.model_type 匹配

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        """将安全检测结果作为威胁分析报告输出(简化格式)。

        先判断事件类型是否为预期的安全检测事件,非安全检测事件
        直接返回无风险报告,避免误报。安全检测事件则将其风险信息
        作为威胁分析报告输出。

        model_data 为事件描述 json(由 BlankDataModeler 透传),
        安全检测字段(event_class、risk_source、risk_type、risk_level、
        evidence 等)嵌套在 event_node 子字典中。

        Args:
            model_data: 建模数据(此处为事件描述 json,由 BlankDataModeler 透传),
                包含 event_node、aux_ids、trace 等字段。安全检测事件的
                event_node 中包含 event_class、risk_source、risk_type、
                risk_level、risk_assessment 等字段。

        Returns:
            威胁分析报告(简化格式)。安全检测事件返回包含风险信息的报告,
            非安全检测事件返回无风险报告。
        """
        # 先判断事件类型是否为预期的安全检测事件
        if not isinstance(model_data, dict):
            return self._empty_report()

        # 安全检测字段嵌套在 event_node 子字典中
        # 兼容顶层和 event_node 两种位置,增强健壮性
        event_node = model_data.get("event_node", {})
        if not isinstance(event_node, dict):
            event_node = {}

        event_class = event_node.get("event_class") or model_data.get(
            "event_class", ""
        )
        if event_class != "security":
            # 非安全检测事件,跳过,避免误报
            logger.debug(
                "SecurityRailAnalyzer 跳过非安全检测事件: event_class=%s",
                event_class,
            )
            return self._empty_report()

        # 从 event_node 中提取安全检测结果字段
        risk_level = event_node.get("risk_level") or model_data.get(
            "risk_level", "medium"
        )
        risk_type = event_node.get("risk_type") or model_data.get("risk_type", "")
        risk_source = event_node.get("risk_source") or model_data.get(
            "risk_source", ""
        )

        # evidence 自定义字段:包含风险来源和决策信息(见 9.4.2 节)
        # decision 优先从 risk_assessment 中获取
        risk_assessment = event_node.get("risk_assessment")
        decision = ""
        if isinstance(risk_assessment, dict):
            decision = risk_assessment.get("decision", "")
        if not decision:
            decision = model_data.get("decision", "")
        evidence = {"risk_source": risk_source, "decision": decision}

        logger.info(
            "SecurityRailAnalyzer 上报安全检测事件: risk_type=%s, risk_level=%s, risk_source=%s",
            risk_type,
            risk_level,
            risk_source,
        )
        return {
            "has_risk": True,
            "risk_level": risk_level,
            "risk_type": risk_type,
            "risk_score": self._level_to_score(risk_level),
            "confidence": 1.0,
            "detected_threats": [risk_type] if risk_type else [],
            "analytic_name": "Tool Permission Denied",
            "description": f"PermissionInterruptRail denied the tool call to {event_node.get('action_name', '')}",
            "evidence": evidence,
            "module_name": "security_rail_detection",
        }

    @staticmethod
    def _empty_report() -> dict[str, Any]:
        """非预期事件类型的跳过报告(无风险)。

        Returns:
            无风险的威胁分析报告(简化格式)。
        """
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "risk_score": 0.0,
            "confidence": 1.0,
            "detected_threats": [],
            "analytic_name": "Pass Through Scan",
            "description": "No risk detected",
            "evidence": {},
            "module_name": "security_rail_detection",
        }

    @staticmethod
    def _level_to_score(risk_level: str) -> float:
        """将风险等级字符串映射为风险分数。

        映射规则:safe=0, low=25, medium=50, high=75, critical=100。
        未知等级回退为 50(medium 级别)。

        Args:
            risk_level: 风险等级字符串,如 "high"、"medium"。

        Returns:
            风险分数(0-100)。
        """
        mapping = {
            "safe": 0.0,
            "low": 25.0,
            "medium": 50.0,
            "high": 75.0,
            "critical": 100.0,
        }
        return mapping.get(risk_level, 50.0)
