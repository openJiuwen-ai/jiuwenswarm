# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 空白数据建模插件。

BlankDataModeler 是当前 0.1 版本的默认数据建模插件,
不做任何处理,直接将事件描述 json 作为建模数据传递给威胁分析插件,
用于测试检测模块的流水线是否跑通。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BlankDataModeler:
    """空白数据建模插件。

    不做任何处理,直接将事件描述 json 作为建模数据传递给威胁分析插件。
    用于测试检测模块的流水线是否跑通,也可作为占位插件供未实现建模逻辑的
    检测模块使用,确保事件能顺畅流转到配对的分析插件。
    """

    name: str = "BlankDataModeler"
    model_type: str = "blank"  # 与 BlankThreatAnalyzer.expected_model_type 匹配

    async def build_model(self, event_desc: dict[str, Any]) -> dict[str, Any]:
        """构建建模数据。

        直接返回事件描述 json,不做任何处理。BlankThreatAnalyzer 的
        expected_model_type 同为 "blank",可无缝消费该建模数据。

        Args:
            event_desc: 事件描述 json,包含 event_node、aux_ids、trace 等字段。

        Returns:
            与入参相同的事件描述 json,作为建模数据。

        Raises:
            TypeError: event_desc 不是 dict 类型时。
        """
        if not isinstance(event_desc, dict):
            raise TypeError(
                f"event_desc 必须为 dict,实际类型: {type(event_desc).__name__}"
            )

        logger.debug(
            "BlankDataModeler 直接透传事件描述: keys=%s",
            list(event_desc.keys()),
        )
        return event_desc
