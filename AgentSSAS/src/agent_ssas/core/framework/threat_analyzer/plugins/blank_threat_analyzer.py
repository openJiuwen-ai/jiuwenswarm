# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 空白威胁分析插件。

BlankThreatAnalyzer 是当前 0.1 版本的默认威胁分析插件,
不做任何检测,直接返回"无风险"的威胁分析报告,
用于测试检测模块的流水线是否跑通。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BlankThreatAnalyzer:
    """空白威胁分析插件。

    不做任何检测,直接返回"无风险"的威胁分析报告。
    用于测试检测模块的流水线是否跑通,也可作为占位插件供未实现分析逻辑的
    检测模块使用,确保流水线末端总能产出结构一致的报告。

    expected_model_type 与 BlankDataModeler.model_type 同为 "blank",
    二者配对组成默认的"透传"检测模块。
    """

    name: str = "BlankThreatAnalyzer"
    expected_model_type: str = "blank"  # 与 BlankDataModeler.model_type 匹配

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        """分析建模数据并返回威胁分析报告。

        不做任何检测,直接返回无风险的威胁分析报告(简化格式)。
        对 model_data 不做类型校验和内容检查,任何输入都视为无风险。

        Args:
            model_data: 配对的数据建模插件输出的建模数据,此处不使用。

        Returns:
            无风险的威胁分析报告(简化格式),包含 has_risk、risk_level、
            risk_type、risk_score、confidence、detected_threats、
            evidence、module_name 等字段。
        """
        logger.debug(
            "BlankThreatAnalyzer 直接返回无风险报告: model_data_type=%s",
            type(model_data).__name__,
        )
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
            "module_name": "",
        }
