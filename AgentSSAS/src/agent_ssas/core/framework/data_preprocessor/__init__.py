# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据预处理模块。

将 AgentSSASSecurityRail 上报的 raw_event 转换为引擎内部统一的 UnifiedEvent,
并管理聚合事件栈。订阅列表管理由检测模块管理器负责。
"""

from agent_ssas.core.framework.data_preprocessor.event_aggregator import EventAggregator
from agent_ssas.core.framework.data_preprocessor.interfaces import EventParserProtocol
from agent_ssas.core.framework.data_preprocessor.agent_preprocessor import (
    AgentSSASPreprocessor,
)
from agent_ssas.core.framework.data_preprocessor.preprocessor import DataPreprocessor

__all__ = [
    "DataPreprocessor",
    "EventAggregator",
    "EventParserProtocol",
    "AgentSSASPreprocessor",
]
