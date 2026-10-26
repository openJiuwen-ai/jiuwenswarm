# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from abc import abstractmethod

from jiuwenswarm.extensions.sdk.base import BaseExtension
from jiuwenswarm.gateway.routing.third_agent import ThirdAgent


class ThirdAgentExtension(BaseExtension):
    """扩展入口：持有真正的 `ThirdAgent` 实现，通过 `get_third_agent()` 暴露。"""

    @abstractmethod
    def get_third_agent(self) -> ThirdAgent:
        """返回第三方 Agent list/switch 能力实例。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭"""
        pass
