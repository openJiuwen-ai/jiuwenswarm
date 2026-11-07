# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁分析框架预置插件。

此目录下只放框架预置的组件(如 BlankThreatAnalyzer)。
检测模块专属的分析插件放在 detection_modules/<module_name>/ 下。
"""

from agent_ssas.core.framework.threat_analyzer.plugins.blank_threat_analyzer import (
    BlankThreatAnalyzer,
)

__all__ = [
    "BlankThreatAnalyzer",
]
