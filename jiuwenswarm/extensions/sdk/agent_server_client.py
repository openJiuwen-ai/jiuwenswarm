from abc import abstractmethod

from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.extensions.sdk.base import BaseExtension


class AgentServerClientExtension(BaseExtension):
    """扩展入口：持有真正的 `AgentServerClient` 实现，通过 `get_client()` 暴露。"""

    @abstractmethod
    def get_client(self) -> AgentServerClient:
        """返回与 AgentServer 通信使用的客户端实例。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭"""
        pass
