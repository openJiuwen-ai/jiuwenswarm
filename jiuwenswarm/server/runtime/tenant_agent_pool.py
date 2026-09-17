from __future__ import annotations

import logging
from typing import Any, ClassVar

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.runtime.agent_manager import AgentManager

logger = logging.getLogger(__name__)


class TenantAgentPool:
    """AgentManager 管理器（单例）.

    职责：
    1. 管理 AgentManager 实例的创建和生命周期
    2. 提供统一的函数调用接口
    3. 调用 AgentManager 的方法（简单分发）
    """

    _instance: ClassVar[TenantAgentPool | None] = None

    def __init__(self) -> None:
        # 单个 AgentManager 实例
        self._agent_manager = AgentManager()
        logger.info("[TenantAgentPool] Initialized with AgentManager")

    @classmethod
    def get_instance(cls) -> "TenantAgentPool":
        """获取单例实例."""
        if cls._instance is None:
            cls._instance = cls()
            logger.info("[TenantAgentPool] Created singleton instance")
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）."""
        if cls._instance is not None:
            logger.info("[TenantAgentPool] Resetting singleton instance")
        cls._instance = None

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """处理非流式请求（简单分发到 AgentManager）.

        Args:
            request: AgentRequest 对象

        Returns:
            AgentResponse 对象
        """
        try:
            logger.info(
                "[TenantAgentPool] process_message called | request_id=%s | channel_id=%s",
                request.request_id,
                request.channel_id,
            )
            return await self._agent_manager.process_message(request)
        except Exception as e:
            logger.error(f"[TenantAgentPool] Error in process_message: {e}", exc_info=True)
            # 可以选择返回错误响应或重新抛出异常
            raise

    async def process_message_stream(self, request: AgentRequest):
        """处理流式请求（简单分发到 AgentManager）.

        Args:
            request: AgentRequest 对象

        Yields:
            AgentResponseChunk 对象
        """
        try:
            logger.info(
                "[TenantAgentPool] process_message_stream called | request_id=%s | channel_id=%s",
                request.request_id,
                request.channel_id,
            )
            async for chunk in self._agent_manager.process_message_stream(request):
                yield chunk
        except Exception as e:
            logger.error(f"[TenantAgentPool] Error in process_message_stream: {e}", exc_info=True)
            raise

    async def cleanup(self) -> None:
        """清理资源."""
        logger.info("[TenantAgentPool] Cleaning up...")
        await self._agent_manager.cleanup()
        logger.info("[TenantAgentPool] Cleanup complete")
