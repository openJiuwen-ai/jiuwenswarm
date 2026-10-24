# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁分析模块。

输入建模数据,输出威胁分析报告。
包含驱动器和框架预置插件实现(如 BlankThreatAnalyzer)。
"""

from agent_ssas.core.framework.threat_analyzer.analyzer import ThreatAnalyzer
from agent_ssas.core.framework.threat_analyzer.interfaces import ThreatAnalyzerPlugin

__all__ = [
    "ThreatAnalyzer",
    "ThreatAnalyzerPlugin",
]
