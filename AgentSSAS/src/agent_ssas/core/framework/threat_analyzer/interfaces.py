# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁分析模块的插件接口协议。

定义 ThreatAnalyzerPlugin,作为威胁分析插件接口。
输入建模数据,输出威胁分析报告(简化格式)。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ThreatAnalyzerPlugin(Protocol):
    """威胁分析插件接口。

    输入一个特定格式的建模数据,
    输出一个特定格式的威胁分析报告。
    """

    name: str
    expected_model_type: str  # 与建模插件的 model_type 匹配

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        """分析建模数据并返回威胁分析报告。

        参数 model_data 为配对的数据建模插件输出的建模数据。
        返回威胁分析报告(简化格式),包含 has_risk、risk_level、
        risk_type、risk_score、confidence、detected_threats、evidence 等字段。
        """
        ...
