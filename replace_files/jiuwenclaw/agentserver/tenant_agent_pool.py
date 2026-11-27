from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.utils import AsyncLRUCache, get_multi_tenant_user_workspace_dir

logger = logging.getLogger(__name__)


class TenantAgentPool:
    """多租户 AgentManager 管理器（单例）.

    根据 agent_id + service_id 维护多个 AgentManager 实例，实现租户隔离。
    每个 AgentManager 管理该租户内的多个 Agent 实例（按 channel_id 区分）。

    service_id 由 chat_id + bot_app_id 组合而成。
    """

    _instance: ClassVar[TenantAgentPool | None] = None

    def __init__(self, cache_max_size: int = 100, cache_ttl: int = 600) -> None:
        # LRU 缓存: key=agent_id+service_id, value=AgentManager 实例
        self._agent_wrappers = AsyncLRUCache(max_size=cache_max_size, ttl_seconds=cache_ttl)
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._global_lock = asyncio.Lock()

        # 保存全局最新配置，用于后续创建 AgentManager 时传递
        self._latest_config: Any = None
        self._latest_env: Any = None

    @classmethod
    def get_instance(cls) -> "TenantAgentPool":
        """获取单例实例."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # pylint: disable=protected-access
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）."""
        if cls._instance:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(cls._instance._agent_wrappers.clear())
            except RuntimeError:
                asyncio.run(cls._instance._agent_wrappers.clear())
            cls._instance._locks.clear()
            cls._instance._lock_loops.clear()
        cls._instance = None

    def _get_lock(self, cache_key: str) -> asyncio.Lock:
        current_loop = asyncio.get_running_loop()

        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()
            self._lock_loops[cache_key] = current_loop
        else:
            stored_loop = self._lock_loops.get(cache_key)
            if stored_loop is not current_loop:
                # 事件循环发生变化，需要重建锁
                old_lock = self._locks[cache_key]
                if old_lock.locked() or getattr(old_lock, '_waiters', None):
                    logger.error(
                        "[TenantAgentPool] Lock for %s has waiters during loop change! "
                        "This may cause data inconsistency.",
                        cache_key
                    )
                self._locks[cache_key] = asyncio.Lock()
                self._lock_loops[cache_key] = current_loop

        return self._locks[cache_key]

    @staticmethod
    def build_service_id(chat_id: str | None, bot_app_id: str | None) -> str:
        """根据 chat_id 和 bot_app_id 构建 service_id.

        Args:
            chat_id: 聊天ID
            bot_app_id: Bot应用ID

        Returns:
            组合后的 service_id，格式: "{chat_id}_{bot_app_id}"
            如果任一参数为空，使用 "unknown" 替代
        """
        chat = chat_id or "unknown_chat_id"
        bot = bot_app_id or "unknown_bot_app_id"
        return f"{chat}_{bot}"

    async def initialize(self, channel_id: str = "", extra_config: dict[str, Any] | None = None) \
            -> dict[str, Any] | None:
        """初始化默认租户的 AgentManager.

        主要用于 ACP 通道的初始化。

        Args:
            channel_id: 通道 ID
            extra_config: 额外配置

        Returns:
            对于 ACP 通道，返回 capabilities；否则返回 None
        """
        agent_id, service_id = "acp", "global_acp"
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        return await agent_manager.initialize(channel_id, extra_config)

    def get_client_capabilities(self, channel_id: str = "") -> dict[str, Any]:
        """获取默认租户的客户端能力.

        Args:
            channel_id: 通道 ID

        Returns:
            客户端能力字典
        """
        agent_manager = self._get_agent_manager_nowait("acp", "global_acp")
        if agent_manager is None:
            return {}
        return agent_manager.get_client_capabilities(channel_id)

    async def create_session(self, channel_id: str = "", session_id: str | None = None) -> str:
        """创建会话.

        Args:
            channel_id: 通道 ID
            session_id: 可选的会话 ID

        Returns:
            会话 ID
        """
        agent_id, service_id = "acp", "global_acp"
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        return await agent_manager.create_session(channel_id, session_id)

    async def cleanup(self) -> None:
        """清理所有缓存的 AgentManager 实例（用于 shutdown 或重置）."""
        keys = await self._agent_wrappers.keys()
        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is not None:
                try:
                    await agent_manager.cleanup()
                except Exception as e:
                    logger.warning("[TenantAgentPool] AgentManager cleanup failed for %s: %s", key, e)

        await self._agent_wrappers.clear()
        self._locks.clear()
        self._lock_loops.clear()
        logger.info("[TenantAgentPool] All agent managers and states cleaned up")

    def is_working(self) -> bool:
        """返回是否有任何租户 Agent 正在工作.

        任意租户在工作则返回 True，立即短路返回。
        """
        # 直接访问内部缓存
        cache = self._agent_wrappers._cache
        for key, (agent_manager, _) in cache.items():
            if agent_manager is not None:
                try:
                    if agent_manager.is_working():
                        return True
                except Exception as e:
                    logger.warning("Get working status failed for key=%s: %s", key, e)
                    continue
        return False

    async def cancel_all_inflight_work(self, reason: str = "[gateway ws disconnect] ") -> None:
        """WebSocket 断开时：对每个已缓存 ``AgentManager`` 取消在途任务。"""
        keys = await self._agent_wrappers.keys()
        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is None:
                continue
            try:
                await agent_manager.cancel_all_inflight_work(reason)
            except Exception:
                logger.exception(
                    "[TenantAgentPool] cancel_all_inflight_work failed for key=%s", key
                )

    async def reload_agents_config(self, config: Any, env: Any) -> None:
        """与 ``AgentManager.reload_agents_config`` 一致：对每个已缓存租户热重载配置。"""
        # 保存配置，用于后续创建的 AgentManager 传递
        self._latest_config = config
        self._latest_env = env

        keys = await self._agent_wrappers.keys()
        if not keys:
            logger.info("[TenantAgentPool] No AgentManager instances yet, config saved for future creation")
            return

        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is None:
                continue
            try:
                await agent_manager.reload_agents_config(config, env)
            except Exception:
                logger.exception(
                    "[TenantAgentPool] reload_agents_config failed for key=%s", key
                )

    async def _ensure_agent_manager(
            self,
            agent_id: str,
            service_id: str | None = None,
    ) -> Any:
        """确保 agent_id + service_id 对应的 AgentManager 实例已创建（线程安全）.

        Args:
            agent_id: agent名称/路径
            service_id: 服务ID（chat_id + bot_app_id 组合）

        Returns:
            AgentManager 实例
        """
        cache_key = f"{agent_id}_{service_id}"
        lock = self._get_lock(cache_key)

        async with lock:
            agent_manager = await self._agent_wrappers.get(cache_key)
            if agent_manager is not None:
                return agent_manager

            logger.info(
                "[session=%s] [TenantAgentPool] 创建新 AgentManager 实例: agent_id=%s, service_id=%s",
                service_id,
                agent_id,
                service_id,
            )

            try:
                # 设置工作目录隔离
                agent_dir_path = get_multi_tenant_user_workspace_dir(service_id, agent_id)

                from jiuwenclaw.agentserver.agent_manager import AgentManager

                # 创建新的 AgentManager 实例，传入保存的配置
                agent_manager = AgentManager(
                    agent_id=agent_id,
                    service_id=service_id,
                    user_workspace_dir=agent_dir_path,
                    config_base=self._latest_config,
                    env_overrides=self._latest_env,
                )

                # 存入 LRU 缓存
                await self._agent_wrappers.put(cache_key, agent_manager)

                # 清理无用的锁（防止内存泄漏）
                active_keys = await self._agent_wrappers.keys()
                stale_locks = [k for k in self._locks if k not in active_keys]
                for stale_key in stale_locks:
                    del self._locks[stale_key]
                    self._lock_loops.pop(stale_key, None)

                logger.info(
                    "[session=%s] [TenantAgentPool] AgentManager 实例创建完成: agent_id=%s, service_id=%s",
                    service_id,
                    agent_id,
                    service_id,
                )
                return agent_manager
            except Exception as e:
                logger.error("[TenantAgentPool] 创建 AgentManager 失败: %s", e)
                raise

    def _get_agent_manager_nowait(self, agent_id: str, service_id: str) -> Any | None:
        """同步获取 AgentManager 实例（不自动创建）.

        Args:
            agent_id: agent名称/路径
            service_id: 服务ID

        Returns:
            AgentManager 实例或 None
        """
        cache_key = f"{agent_id}_{service_id}"
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._agent_wrappers.get(cache_key),
                loop
            )
            return future.result(timeout=1)
        except Exception:
            return None

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """处理非流式请求."""
        agent_id, service_id = self.extract_ids(request)
        from jiuwenclaw.agentserver.checkpoint_setup import ensure_persistent_checkpointer

        await ensure_persistent_checkpointer(service_id, agent_id)
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        return await agent_manager.process_message(request)

    async def process_message_stream(
            self, request: AgentRequest
    ):
        """处理流式请求."""
        agent_id, service_id = self.extract_ids(request)
        from jiuwenclaw.agentserver.checkpoint_setup import ensure_persistent_checkpointer

        await ensure_persistent_checkpointer(service_id, agent_id)
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        async for chunk in agent_manager.process_message_stream(request):
            yield chunk

    @staticmethod
    def extract_ids(request: AgentRequest):
        """从请求中提取 agent_id 和 service_id.

        单租户作为多租户的默认特例：
        - 无指定时返回 ('default', 'default')
        - 有指定时返回对应值
        - ACP 通道返回 ('acp', 'global_acp')
        """
        agent_id = getattr(request, 'agent_id', None)
        service_id = getattr(request, 'service_id', None)

        # ACP 通道使用固定租户
        if request.channel_id == "acp":
            return "acp", "global_acp"

        # 无指定时使用默认租户（单租户作为多租户的默认特例）
        agent_id = agent_id or "default"
        service_id = service_id or getattr(request, 'session_id', None) or "default"

        return agent_id, service_id

    async def reload_agent_config(
            self,
            agent_id: str,
            config_base: Any = None,
            env_overrides: dict | None = None
    ) -> None:
        """重新加载指定租户的 Agent 配置."""
        service_id = "global_acp" if agent_id == "acp" else "default"
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        channel_id = "acp" if agent_id == "acp" else "default"
        await agent_manager.reload_agent_config(
            channel_id=channel_id,
            config_base=config_base,
            env_overrides=env_overrides
        )

    async def get_agent_count(self) -> int:
        """获取当前活跃的 AgentManager 实例数量."""
        return self._agent_wrappers.__len__()

    async def get_agent_manager(self, agent_id: str, service_id: str) -> Any:
        """获取指定租户的 AgentManager 实例.

        Args:
            agent_id: agent名称/路径
            service_id: 服务ID

        Returns:
            AgentManager 实例
        """
        return await self._ensure_agent_manager(agent_id, service_id)

    def get_agent_manager_nowait(self, agent_id: str, service_id: str) -> Any | None:
        """同步获取 AgentManager 实例（不自动创建）.

        Args:
            agent_id: agent名称/路径
            service_id: 服务ID

        Returns:
            AgentManager 实例或 None
        """
        return self._get_agent_manager_nowait(agent_id, service_id)

    async def cancel_all_inflight_work(self, reason: str = "[gateway ws disconnect] ") -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时：取消所有租户的在途任务。

        遍历所有缓存的 AgentManager 实例，逐个调用其 cancel_all_inflight_work 方法。
        单个租户失败不影响其他租户的取消操作，记录异常日志

        Args:
            reason: 取消原因描述
        """
        keys = await self._agent_wrappers.keys()
        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is not None:
                try:
                    await agent_manager.cancel_all_inflight_work(reason)
                except Exception:
                    logger.exception(
                        "[TenantAgentPool] cancel_all_inflight_work failed for %s", key
                    )
