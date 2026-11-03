# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""ThirdAgent 扩展入口契约。

归属：接口 ABC 不进 ``openjiuwen_protocol``（协议仓只含纯数据），由 gateway
实现侧持有；``ThirdAgent`` 的真源在 ``gateway.common.client.third_agent``。
"""

from __future__ import annotations

from abc import abstractmethod

from gateway.common.client.third_agent import ThirdAgent
from gateway.extensions.sdk.base import BaseExtension


class ThirdAgentExtension(BaseExtension):
    """扩展入口：持有真正的 `ThirdAgent` 实现，通过 `get_third_agent()` 暴露。"""

    @abstractmethod
    def get_third_agent(self) -> ThirdAgent:
        """返回第三方 Agent list/switch 能力实例。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭"""
        pass


__all__ = ["ThirdAgentExtension"]
