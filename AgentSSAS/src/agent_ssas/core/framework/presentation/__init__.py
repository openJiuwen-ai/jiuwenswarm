# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 呈现模块。

输入威胁分析报告(简化格式),基于 aux_ids、event_node 等信息
构建完整的 OCSF Detection Finding 格式报告并输出。
"""

from agent_ssas.core.framework.presentation.interfaces import PresentationPlugin
from agent_ssas.core.framework.presentation.ocsf import build_ocsf_report
from agent_ssas.core.framework.presentation.threat_log import AgentSSASThreatLog

__all__ = [
    "AgentSSASThreatLog",
    "PresentationPlugin",
    "build_ocsf_report",
]
