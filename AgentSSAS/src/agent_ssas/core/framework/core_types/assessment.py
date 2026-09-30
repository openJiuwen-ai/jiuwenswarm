# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 风险评估数据模型。

定义五级风险分类枚举 RiskLevel 和安全分析的统一输出格式 RiskAssessment。
RiskAssessment 是多个检测模块对同一事件的检测结果聚合,
由流水线模块 ThreatAnalysisPipeline 执行聚合。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# 风险等级 → 数值序号的映射,供比较运算符使用。
# 模块级常量避免每次比较重复创建列表。
_RISK_ORDER: dict[str, int] = {
    "safe": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class RiskLevel(str, enum.Enum):
    """五级风险分类,贯穿检测、决策、上报全流程。

    等级顺序:SAFE < LOW < MEDIUM < HIGH < CRITICAL。
    RiskAssessment.risk_level 驱动 AgentSSASSecurityRail 侧的决策映射。
    """

    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def _rank(self) -> int:
        """获取当前风险等级的数值序号(0-4)。"""
        return _RISK_ORDER.get(self.value, 0)

    def __ge__(self, other: RiskLevel) -> bool:
        """大于等于比较,基于等级顺序。"""
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self._rank() >= other._rank()

    def __gt__(self, other: RiskLevel) -> bool:
        """大于比较,基于等级顺序。"""
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self._rank() > other._rank()

    def __le__(self, other: RiskLevel) -> bool:
        """小于等于比较,基于等级顺序。"""
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self._rank() <= other._rank()

    def __lt__(self, other: RiskLevel) -> bool:
        """小于比较,基于等级顺序。"""
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self._rank() < other._rank()


@dataclass
class RiskAssessment:
    """安全分析的统一输出格式:多个检测模块报告的聚合结果。

    由流水线模块 ThreatAnalysisPipeline 将所有已订阅该事件的检测模块
    各自输出的威胁分析报告聚合为单个结果,由接入适配模块通过
    report_event 返回给 AgentSSASSecurityRail。

    RiskAssessment → SecurityDecision 的映射在 AgentSSASSecurityRail 侧实现。
    """

    has_risk: bool = False
    risk_level: RiskLevel = RiskLevel.SAFE
    risk_type: str = ""
    risk_score: float = 0.0
    confidence: float = 0.0
    detected_threats: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=lambda: ["log"])
    details: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict,risk_level 转换为字符串值。"""
        return {
            "has_risk": self.has_risk,
            "risk_level": self.risk_level.value,
            "risk_type": self.risk_type,
            "risk_score": self.risk_score,
            "confidence": self.confidence,
            "detected_threats": self.detected_threats,
            "recommended_actions": self.recommended_actions,
            "details": self.details,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RiskAssessment:
        """从 dict 反序列化,risk_level 从字符串值转换为枚举。"""
        level_str = data.get("risk_level", "safe")
        try:
            level = RiskLevel(level_str)
        except (ValueError, TypeError):
            level = RiskLevel.SAFE
        return cls(
            has_risk=data.get("has_risk", False),
            risk_level=level,
            risk_type=data.get("risk_type", ""),
            risk_score=data.get("risk_score", 0.0),
            confidence=data.get("confidence", 0.0),
            detected_threats=data.get("detected_threats", []),
            recommended_actions=data.get("recommended_actions", ["log"]),
            details=data.get("details", {}),
            evidence=data.get("evidence", {}),
        )
