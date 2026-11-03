# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""AgentServerClient 扩展入口契约。

归属：接口 ABC 不进 ``openjiuwen_protocol``（协议仓只含纯数据），由 gateway
实现侧持有；``AgentServerClient`` 的真源在 ``gateway.common.client.agent_client``。

实现方各自持有具体 client，跨仓实例经组装期注入传递。
"""

from __future__ import annotations

from abc import abstractmethod

from gateway.common.client.agent_client import AgentServerClient
from gateway.extensions.sdk.base import BaseExtension


class AgentServerClientExtension(BaseExtension):
    """扩展入口：持有真正的 `AgentServerClient` 实现，通过 `get_client()` 暴露。"""

    @abstractmethod
    def get_client(self) -> AgentServerClient:
        """返回与 AgentServer 通信使用的客户端实例。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭"""
        pass


__all__ = ["AgentServerClientExtension"]
