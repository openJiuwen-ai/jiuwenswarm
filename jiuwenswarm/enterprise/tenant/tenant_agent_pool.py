# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""多租户 AgentManager 池，按 ``tenant_key`` 缓存实例.

``tenant_key = md5("{group_id}::{bot_id}::{user_id}")``（见 ``build_tenant_key``）。
缺省字段补 ``\"default\"``，个人版在三元组缺失时仍可兼容为单一逻辑租户。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.enterprise.tenant.tenant_paths import (
    build_tenant_key,
    get_multi_tenant_user_workspace_dir,
    normalize_tenant_key_part,
    seed_tenant_agent_workspace,
)
from jiuwenswarm.enterprise import is_enterprise
from jiuwenswarm.enterprise.tenant.async_lru_cache import AsyncLRUCache
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.server.runtime.agent_manager import AgentManager

logger = logging.getLogger(__name__)


class TenantAgentPool:
    """多租户 AgentManager 管理器，由所属 Runtime 创建并清理.

    职责：
    1. 按 ``tenant_key`` 用 LRU 创建 / 缓存多个 ``AgentManager``
    2. 请求进入时绑定 ``TenantContext``，再分发到对应 Manager
    3. 管理实例生命周期（cleanup / 测试重置）

    Runtime uses an owned pool so closing one Runtime cannot clear another.
    The legacy singleton helpers remain available to compatibility callers.

    每个 AgentManager 仍管理该租户内多个 Agent（按 channel_id 等区分）。
    """

    _instance: ClassVar[TenantAgentPool | None] = None

    def __init__(
        self,
        cache_max_size: int | None = None,
        cache_ttl: int | None = None,
    ) -> None:
        # LRU 缓存: key=tenant_key, value=AgentManager
        # 默认 None：不限制容量与 TTL，避免长阻塞（如 ask_user_question）期间误淘汰
        self._agent_wrappers = AsyncLRUCache(
            max_size=cache_max_size,
            ttl_seconds=cache_ttl,
        )
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._lock_loops: dict[Hashable, asyncio.AbstractEventLoop] = {}
        self._global_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "TenantAgentPool":
        """获取单例实例."""
        if cls._instance is None:
            cls._instance = cls()
            logger.info("[TenantAgentPool] Created singleton instance")
        return cls._instance

    @classmethod
    def peek_instance(cls) -> "TenantAgentPool | None":
        """返回已初始化的单例；若尚未创建则返回 None（不触发构造）."""
        return cls._instance

    def reset_runtime_state(self) -> None:
        """清空 LRU 与创建锁（测试 / 单例重置用公开入口）."""
        try:
            # 避免 asyncio.run / 残留 task 在 pytest 下触发 unclosed loop
            self._agent_wrappers = AsyncLRUCache(
                max_size=None,
                ttl_seconds=None,
            )
        except Exception:
            logger.debug(
                "[TenantAgentPool] reset_runtime_state cache replace failed",
                exc_info=True,
            )
        self._locks.clear()
        self._lock_loops.clear()

    async def touch_manager_if_same(
        self, tenant_key: str, manager: AgentManager
    ) -> bool:
        """若池中仍是同一 Manager，则续约 LRU 访问时间."""
        return await self._agent_wrappers.touch_if_same(tenant_key, manager)

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）."""
        if cls._instance is not None:
            logger.info("[TenantAgentPool] Resetting singleton instance")
            cls._instance.reset_runtime_state()
        cls._instance = None

    def _get_lock(self, cache_key: Hashable) -> asyncio.Lock:
        """按 cache_key 取创建锁；若 event loop 已切换则重建锁."""
        current_loop = asyncio.get_running_loop()
        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()
            self._lock_loops[cache_key] = current_loop
        else:
            stored_loop = self._lock_loops.get(cache_key)
            if stored_loop is not current_loop:
                self._locks[cache_key] = asyncio.Lock()
                self._lock_loops[cache_key] = current_loop
        return self._locks[cache_key]

    @staticmethod
    def normalize_part(value: str | None, *, default: str = "default") -> str:
        """归一化三元组字段：空则回落 default."""
        return normalize_tenant_key_part(value, default=default)

    @staticmethod
    def _pool_key(tenant_key: str | None = None) -> str:
        """池缓存键：个人版永远收敛到默认槽；企业版才按租户区分.

        个人版进程内只保留一个 AgentManager，避免 user_id / IM 三元组把内存
        状态拆开，同时落盘仍共用 ``~/.jiuwenswarm``。
        """
        if not is_enterprise():
            return build_tenant_key()
        key = str(tenant_key).strip() if tenant_key is not None else ""
        return key or build_tenant_key()

    @classmethod
    def extract_tenant_key(cls, request: Any) -> str:
        """从请求三元组拼出 tenant_key（有则用、无则 default）.

        个人版忽略请求身份，固定返回默认 ``build_tenant_key()``，保证单 Manager。
        实现见 :mod:`jiuwenswarm.enterprise.tenant.tenant_identity`（Front 控制面可安全复用，不拉 AgentManager）。
        """
        from jiuwenswarm.enterprise.tenant.tenant_identity import extract_tenant_key

        return extract_tenant_key(request)

    @classmethod
    def extract_triple(cls, request: Any) -> tuple[str, str, str]:
        """从请求提取 (group_id, bot_id, user_id)，缺省补 default.

        见 :func:`jiuwenswarm.enterprise.tenant.tenant_identity.extract_triple`。
        """
        from jiuwenswarm.enterprise.tenant.tenant_identity import extract_triple

        return extract_triple(request)

    async def _ensure_agent_manager(self, tenant_key: str) -> AgentManager:
        """确保 ``tenant_key`` 对应的 AgentManager 已创建（双检锁 + LRU）."""
        cache_key = tenant_key
        lock = self._get_lock(cache_key)
        async with lock:
            cached = await self._agent_wrappers.get(cache_key)
            if cached is not None:
                return cached

            logger.info(
                "[TenantAgentPool] creating AgentManager tenant_key=%s enterprise=%s",
                tenant_key,
                is_enterprise(),
            )
            # 企业版：懒建 workspace_{tenant_key} 并补种模板；个人版路径不变，无需播种
            if is_enterprise():
                tenant_root = get_multi_tenant_user_workspace_dir(tenant_key)
                seed_tenant_agent_workspace(tenant_root)

            manager = AgentManager()
            await self._agent_wrappers.put(cache_key, manager)

            # LRU 淘汰后清理对应创建锁，避免锁表无限增长
            async with self._global_lock:
                active_keys = await self._agent_wrappers.keys()
                stale = [k for k in self._locks if k not in active_keys]
                for stale_key in stale:
                    self._locks.pop(stale_key, None)
                    self._lock_loops.pop(stale_key, None)

            return manager

    async def adopt_manager(
        self, tenant_key: str, manager: AgentManager
    ) -> AgentManager:
        """若槽位为空，将已有 Manager 登记到 ``tenant_key``（不覆盖）.

        供启动路径兼容；新路径优先 :meth:`put_nowait` 在 ``AgentRuntime.__init__``
        同步登记默认租户。个人版始终落到默认槽。
        """
        key = self._pool_key(tenant_key)
        lock = self._get_lock(key)
        async with lock:
            cached = await self._agent_wrappers.get(key)
            if cached is not None:
                return cached
            await self._agent_wrappers.put(key, manager)
            return manager

    def put_nowait(
        self, tenant_key: str | None, manager: AgentManager
    ) -> AgentManager:
        """同步登记 Manager（``__init__`` / 尚不能 await 的路径）.

        若该 key 已有实例则返回已有实例，不覆盖。个人版始终落到默认槽。
        """
        key = self._pool_key(tenant_key)
        cached = self._agent_wrappers.get_nowait(key)
        # 接受 duck-typed Manager（单测 fake），不强制 isinstance(AgentManager)。
        if cached is not None:
            return cached
        self._agent_wrappers.put_nowait(key, manager)
        return manager

    def get_manager_nowait(
        self, tenant_key: str | None = None
    ) -> AgentManager | None:
        """同步读取已有 Manager，不创建. 个人版始终读默认槽."""
        key = self._pool_key(tenant_key)
        cached = self._agent_wrappers.get_nowait(key)
        return cached if cached is not None else None

    async def get_agent_manager(self, tenant_key: str | None = None) -> AgentManager:
        """获取指定租户的 AgentManager（不存在则懒创建）. 个人版始终默认槽."""
        return await self._ensure_agent_manager(self._pool_key(tenant_key))

    async def get_cached_agent_manager(
        self, tenant_key: str | None = None
    ) -> AgentManager | None:
        """异步读取已有 Manager，不自动创建.

        控制面 handler 与缓存同 loop，须 await；不可在同 loop 上同步 future.result。
        个人版始终读默认槽。
        """
        return await self._agent_wrappers.get(self._pool_key(tenant_key))

    @asynccontextmanager
    async def tenant_scope(self, request: Any):
        """绑定 TenantContext，yield 该租户的 AgentManager；退出时 reset 并续约 LRU."""
        tenant_key = self.extract_tenant_key(request)
        ctx = TenantContext.bind(tenant_key)
        try:
            manager = await self._ensure_agent_manager(tenant_key)
            try:
                yield manager
            finally:
                await self._agent_wrappers.touch_if_same(tenant_key, manager)
        finally:
            ctx.reset()

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """处理非流式请求：进租户作用域后分发到对应 AgentManager.

        Args:
            request: AgentRequest 对象

        Returns:
            AgentResponse 对象
        """
        try:
            logger.info(
                "[TenantAgentPool] process_message request_id=%s channel_id=%s "
                "tenant_key=%s",
                request.request_id,
                request.channel_id,
                self.extract_tenant_key(request),
            )
            async with self.tenant_scope(request) as agent_manager:
                return await agent_manager.process_message(request)
        except Exception as e:
            logger.error(
                "[TenantAgentPool] Error in process_message: %s", e, exc_info=True
            )
            raise

    async def process_message_stream(
        self, request: AgentRequest
    ) -> AsyncIterator[Any]:
        """处理流式请求：进租户作用域后分发到对应 AgentManager.

        Args:
            request: AgentRequest 对象

        Yields:
            AgentResponseChunk 对象
        """
        try:
            logger.info(
                "[TenantAgentPool] process_message_stream request_id=%s "
                "channel_id=%s tenant_key=%s",
                request.request_id,
                request.channel_id,
                self.extract_tenant_key(request),
            )
            async with self.tenant_scope(request) as agent_manager:
                async for chunk in agent_manager.process_message_stream(request):
                    yield chunk
        except Exception as e:
            logger.error(
                "[TenantAgentPool] Error in process_message_stream: %s",
                e,
                exc_info=True,
            )
            raise

    async def reload_tenant_config(
        self,
        tenant_key: str,
        config_base: Any = None,
        env_overrides: dict | None = None,
    ) -> None:
        """重新加载指定租户的 Agent 配置（懒创建 Manager 后调用 reload）."""
        manager = await self._ensure_agent_manager(tenant_key)
        ctx = TenantContext.bind(tenant_key)
        try:
            reload = getattr(manager, "reload_agents_config", None)
            if callable(reload):
                await reload(config_base, env_overrides)
        finally:
            ctx.reset()

    async def cleanup(self) -> None:
        """清理所有缓存的 AgentManager（shutdown 或重置）."""
        logger.info("[TenantAgentPool] Cleaning up...")
        # 用 snapshot（不过滤过期、不删条目），避免 keys()/get() 在 TTL 下
        # 先丢掉过期项导致跳过 manager.cleanup()。
        # 接受 duck-typed Manager（单测 fake），不强制 isinstance。
        managers = self._agent_wrappers.snapshot_values_nowait()
        for manager in managers:
            cleanup = getattr(manager, "cleanup", None)
            if not callable(cleanup):
                continue
            try:
                await cleanup()
            except Exception as e:
                logger.warning(
                    "[TenantAgentPool] AgentManager cleanup failed: %s",
                    e,
                )
        await self._agent_wrappers.clear()
        self._locks.clear()
        self._lock_loops.clear()
        logger.info("[TenantAgentPool] Cleanup complete")

    def iter_agent_managers_nowait(self) -> list[AgentManager]:
        """同步快照当前缓存中的 AgentManager（不创建新实例）."""
        return list(self._agent_wrappers.snapshot_values_nowait())

    async def get_agent_count(self) -> int:
        """当前活跃的 AgentManager 实例数量."""
        return len(self._agent_wrappers)
