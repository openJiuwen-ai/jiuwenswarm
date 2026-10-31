# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 到 AgentMoss 的事件建模与结构适配插件。"""

from __future__ import annotations

import logging
from typing import Any

from .event_adapter import MODEL_TYPE, adapt_event_desc

logger = logging.getLogger(__name__)


class AgentMossModeler:
    """AgentMoss 数据建模插件。

    将 AgentSSAS 生命周期事件映射为 AgentMoss 的运行时事件结构，
    同时保留原始 SSAS 字段和显式关联 ID，供配对的 AgentMossAnalyzer 消费。

    model_type 为 "agent_behavior_model"，与 AgentMossAnalyzer 的
    expected_model_type 匹配。
    """

    name: str = "AgentMossModeler"
    model_type: str = MODEL_TYPE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 AgentMossModeler。

        Args:
            config: 插件专有配置参数，当前保留用于兼容模块加载接口。
        """
        self._config = config or {}

    async def build_model(self, event_desc: dict[str, Any]) -> dict[str, Any]:
        """构建 Agent 行为模型。

        执行确定性的事件类型、内容和关联 ID 映射。适配过程不根据
        自然语言内容推断事件依赖关系。

        Args:
            event_desc: 事件描述 json，包含 event_node、aux_ids、trace 等字段。

        Returns:
            行为模型描述 dict，包含兼容字段、agentmoss_event 和
            correlation 子结构。

        Raises:
            TypeError: event_desc 不是 dict 类型时。
        """
        model = adapt_event_desc(event_desc)

        logger.debug(
            "AgentMossModeler 完成事件适配: ssas_event=%s, agentmoss_event=%s",
            model["event_type"],
            model["agentmoss_event"]["event_type"],
        )
        return model
