# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据预处理模块的插件接口协议。

定义 EventParserProtocol,作为事件解析器插件接口。
按 common.source 字段选择对应的格式解析器,将 raw_event 转换为 UnifiedEvent 列表。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_ssas.core.framework.core_types.event import UnifiedEvent


@runtime_checkable
class EventParserProtocol(Protocol):
    """事件解析器插件接口。

    输入为 AgentSSASSecurityRail 上报的 raw_event(三层结构 dict),
    输出为引擎内部统一事件 UnifiedEvent 列表。
    """

    async def parse(self, raw_event: dict) -> list[UnifiedEvent]:
        """解析 raw_event 为 UnifiedEvent 列表。

        参数 raw_event 为原始事件 dict(包含 common / payload / metadata 三层)。
        返回 UnifiedEvent 列表:[派生事件(如有)] + [基础事件],
        供后续检测模块消费。
        """
        ...
