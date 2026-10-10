# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据建模模块的插件接口协议。

定义 DataModelerPlugin,作为数据建模插件接口。
输入事件描述 json(基础事件或聚合事件),输出特定格式的建模数据。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataModelerPlugin(Protocol):
    """数据建模插件接口。

    输入一个事件描述 json(基础事件或聚合事件),
    输出一个特定格式的建模数据。
    """

    name: str
    model_type: str  # 建模数据类型标识,与威胁分析插件的 expected_model_type 匹配

    async def build_model(self, event_desc: dict[str, Any]) -> Any:
        """构建建模数据。

        参数 event_desc 为事件描述 json,包含 event_node、aux_ids、trace 等字段。
        返回特定格式的建模数据,供配对的威胁分析插件消费。
        """
        ...
