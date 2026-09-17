# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 流水线模块。

从检测模块管理器查询订阅列表,遍历执行每条流水线
(建模→分析→存储→呈现),聚合报告为 RiskAssessment。
"""

from agent_ssas.core.framework.analysis_pipeline.pipeline import ThreatAnalysisPipeline

__all__ = [
    "ThreatAnalysisPipeline",
]
