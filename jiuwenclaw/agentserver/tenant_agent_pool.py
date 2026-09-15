from __future__ import annotations

import asyncio
import logging
from collections.abc import Hashable, Iterable
from typing import Any, ClassVar

from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.utils import AsyncLRUCache, get_multi_tenant_user_workspace_dir
from jiuwenclaw.agentserver.reload_result import (
    ReloadAggregateResult,
    log_agent_config_hot_reload,
    log_reload_config_changes,
)
from jiuwenclaw.local_env_config import (
    EnvNsIdError,
    apply_env_removals,
    apply_process_baseline_gaps,
    clear_agent_env_ns,
    effective_tip,
    normalize_env_ns_id,
    replace_active_env,
    stage_env_overrides,
)
from jiuwenclaw.agentserver.sync_agents_configs import (
    AgentSyncResultItem,
    build_agent_result,
    build_agent_spec,
    materialize_sync_env,
    validate_sync_payload,
)
from jiuwenclaw.agentserver.tenant_catalog_registry import TenantCatalogRegistry
from jiuwenclaw.agentserver.deep_agent.rails.skill_credential_injection_rail import (
    coalesce_config_skill_envs,
)
from jiuwenclaw.agentserver.tools.multimodal_config import (
    infer_multimodal_env_removals,
    sync_multimodal_env_omission_state,
)

logger = logging.getLogger(__name__)


def filter_cached_agent_managers(values: Iterable[Any]) -> list[Any]:
    """Return ``AgentManager`` instances from cache snapshot values."""
    from jiuwenclaw.agentserver.agent_manager import AgentManager

    managers: list[Any] = []
    for value in values:
        if isinstance(value, AgentManager):
            managers.append(value)
            continue
        if value is not None:
            logger.warning(
                "[TenantAgentPool] skip non-AgentManager cache entry: type=%s",
                type(value).__name__,
            )
    return managers


class TenantAgentPool:
    """多租户 AgentManager 管理器（单例）.

    根据 agent_id + service_id 维护多个 AgentManager 实例，实现租户隔离。
    每个 AgentManager 管理该租户内的多个 Agent 实例（按 channel_id 区分）。

    service_id 由 chat_id + bot_app_id 组合而成。
    """

    _instance: ClassVar[TenantAgentPool | None] = None

    def __init__(
        self,
        cache_max_size: int | None = 100,
        cache_ttl: int | None = 14400,
    ) -> None:
        # LRU 缓存: key=(agent_id, service_id), value=AgentManager 实例
        # 限制容量与 TTL，避免 AgentManager 实例永驻内存导致内存泄漏。
        # 活跃 AgentManager 通过 _refresh_agent_manager_cache 的 touch 机制保持存活。
        self._agent_wrappers = AsyncLRUCache(
            max_size=cache_max_size,
            ttl_seconds=cache_ttl,
        )
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._lock_loops: dict[Hashable, asyncio.AbstractEventLoop] = {}
        self._global_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._last_sync_revision: dict[str, str] = {}
        self._service_model_cache: dict[str, dict[str, Any]] = {}
        # Thread-safety: _service_model_cache is written under _sync_lock (sync_agents_configs)
        # and read from adapter code in asyncio event loop; safe under single-threaded asyncio.

        self._last_reload_trace_id: str | None = None

    @classmethod
    def get_instance(cls) -> "TenantAgentPool":
        """获取单例实例."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def peek_instance(cls) -> "TenantAgentPool | None":
        """返回已初始化的单例；若尚未创建则返回 None（不触发构造）。"""
        return cls._instance

    # pylint: disable=protected-access
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）."""
        if cls._instance:
            # Avoid asyncio.run / fire-and-forget tasks that leave unclosed loops/sockets
            # under pytest filterwarnings=error. Drop cache by replacing the wrapper map.
            try:
                cls._instance._agent_wrappers = AsyncLRUCache(
                    max_size=None,
                    ttl_seconds=None,
                )
            except Exception:
                logger.debug(
                    "[TenantAgentPool] reset_instance cache replace failed",
                    exc_info=True,
                )
            cls._instance._locks.clear()
            cls._instance._lock_loops.clear()
            cls._instance._last_sync_revision.clear()
            cls._instance._service_model_cache.clear()
        TenantCatalogRegistry.reset_for_tests()
        cls._instance = None

    def _build_service_model_cache(
        self,
        service_id: str,
        models_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a shared model cache dict keyed by alias (preferred) or model_name.

        Each entry is stored under a single key: ``alias`` when present,
        otherwise ``model_name``.  This avoids duplicate Model builds when
        ``_merge_service_model_cache`` / ``_resolve_from_shared_model_cache``
        iterate the cache.

        Returns a dict mapping alias/model_name -> model entry dict (not yet
        resolved into ``Model`` instances; resolution happens in
        ``JiuWenClawDeepAdapter._merge_service_model_cache`` /
        ``_resolve_from_shared_model_cache``).
        """
        cache: dict[str, Any] = {}
        for entry in models_entries:
            mcc = entry.get("model_client_config") or {}
            model_name = str(mcc.get("model_name") or "").strip()
            if not model_name:
                continue
            alias = str(entry.get("alias") or "").strip()
            key = alias if alias else model_name
            if key in cache:
                logger.warning(
                    "[TenantAgentPool] _build_service_model_cache: "
                    "duplicate key=%s in service=%s, last entry wins",
                    key, service_id,
                )
            cache[key] = entry
        self._service_model_cache[service_id] = cache
        logger.info(
            "[TenantAgentPool] built service model cache: service_id=%s entries=%d keys=%d",
            service_id,
            len(models_entries),
            len(cache),
        )
        return cache

    def get_service_model_cache(self, service_id: str) -> dict[str, Any]:
        """Return the shared model cache for ``service_id`` (read-only view).

        Returns an empty dict when ``service_id`` has no cache yet. Callers
        must not mutate the returned dict in place — the cache is written
        exclusively under ``_sync_lock`` in ``sync_agents_configs``.
        """
        return self._service_model_cache.get(service_id, {})

    def _get_lock(self, cache_key: Hashable) -> asyncio.Lock:
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

    def get_agent_nowait(self) -> Any | None:
        """获取默认 Agent 实例（同步，不自动创建）.

        Returns:
            JiuWenClaw 实例或 None
        """
        agent_manager = self._get_agent_manager_nowait("acp", "global_acp")
        if agent_manager is None:
            return None
        return agent_manager.get_agent_nowait()

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
        for agent_manager in self.iter_agent_managers_nowait():
            try:
                if agent_manager.is_working():
                    return True
            except Exception as e:
                logger.warning(
                    "[TenantAgentPool] get working status failed: type=%s error=%s",
                    type(agent_manager).__name__,
                    e,
                )
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

    @staticmethod
    def _reconcile_reload_env_for_tenant(
        *,
        service_id: str,
        agent_id: str,
        env: Any,
        reload_trace_id: str | None = None,
    ) -> dict[str, None]:
        """Multimodal omission reconcile scoped to one ``(service_id, agent_id)`` bag."""
        tip = effective_tip(service_id, agent_id)
        previous_env = dict(tip) if tip else None
        # tip empty: do not pass active_env={} (that skips ns reconcile); always pass sid/aid.
        omission_removals = infer_multimodal_env_removals(
            previous_env,
            env if isinstance(env, dict) else None,
            active_env=previous_env if previous_env else None,
            service_id=service_id,
            agent_id=agent_id,
        )
        if omission_removals:
            apply_env_removals(
                omission_removals,
                service_id=service_id,
                agent_id=agent_id,
            )
            log_agent_config_hot_reload(
                logger,
                reload_trace_id=reload_trace_id,
                phase="env_omission_reconcile",
                source="TenantAgentPool",
                agent_id=agent_id,
                service_id=service_id,
                env_removed_by_omission_keys=sorted(omission_removals.keys()),
            )
        sync_multimodal_env_omission_state(
            omission_removals,
            env if isinstance(env, dict) else None,
            service_id=service_id,
            agent_id=agent_id,
        )
        return omission_removals

    @staticmethod
    def _upsert_reload_catalog(
        *,
        service_id: str,
        agent_id: str,
        config: Any,
    ) -> None:
        """Persist a tenant reload snapshot for later cold-start creation."""
        registry = TenantCatalogRegistry.get_instance()
        existing = registry.get(service_id, agent_id)
        config_snapshot = (
            config
            if isinstance(config, dict)
            else (existing.config if existing is not None else {})
        )
        if existing is not None:
            config_snapshot = coalesce_config_skill_envs(config_snapshot, existing.config)
        runtime_snapshot = existing.runtime if existing is not None else {}
        env_snapshot = effective_tip(service_id, agent_id)
        registry.upsert(
            build_agent_spec(
                service_id=service_id,
                agent_id=agent_id,
                config=config_snapshot,
                env=dict(env_snapshot),
                runtime=runtime_snapshot if isinstance(runtime_snapshot, dict) else {},
                revision=(existing.revision or "") if existing is not None else "",
            )
        )

    async def reload_agents_config(
        self,
        config: Any,
        env: Any,
        *,
        reload_trace_id: str | None = None,
    ) -> ReloadAggregateResult:
        """Broadcast reload: reconcile + stage per cached Manager env ns, then configure each."""
        if reload_trace_id:
            self._last_reload_trace_id = reload_trace_id
        registry = TenantCatalogRegistry.get_instance()
        previous_default = registry.get("default", "default")
        log_reload_config_changes(
            logger,
            env=env,
            config=config,
            previous_config=previous_default.config if previous_default is not None else None,
            reload_trace_id=reload_trace_id,
            source="TenantAgentPool",
        )

        env_dict = env if isinstance(env, dict) else None
        keys = await self._agent_wrappers.keys()

        if not keys:
            self._reconcile_reload_env_for_tenant(
                service_id="default",
                agent_id="default",
                env=env_dict,
                reload_trace_id=reload_trace_id,
            )
            stage_env_overrides(env, service_id="default", agent_id="default")
            self._upsert_reload_catalog(
                service_id="default",
                agent_id="default",
                config=config,
            )
            log_agent_config_hot_reload(
                logger,
                reload_trace_id=reload_trace_id,
                phase="cached",
                source="TenantAgentPool",
                note="No AgentManager instances yet, config saved for future creation",
            )
            return ReloadAggregateResult()

        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is None:
                continue
            sid = str(getattr(agent_manager, "env_service_id", "default"))
            aid = str(getattr(agent_manager, "env_agent_id", "default"))
            self._reconcile_reload_env_for_tenant(
                service_id=sid,
                agent_id=aid,
                env=env_dict,
                reload_trace_id=reload_trace_id,
            )
            stage_env_overrides(env, service_id=sid, agent_id=aid)
            self._upsert_reload_catalog(
                service_id=sid,
                agent_id=aid,
                config=config,
            )

        aggregate = ReloadAggregateResult()
        for key in keys:
            agent_manager = await self._agent_wrappers.get(key)
            if agent_manager is None:
                continue
            try:
                result = await agent_manager.reload_agents_config(
                    config, env, reload_trace_id=reload_trace_id
                )
                aggregate.applied += result.applied
                aggregate.deferred += result.deferred
                aggregate.failed.extend(result.failed)
            except Exception:
                logger.exception(
                    "[TenantAgentPool] reload_agents_config failed for key=%s", key
                )
                aggregate.failed.append({"session": str(key), "error": "tenant reload failed"})
        return aggregate

    async def reload_tenant_config(
        self,
        agent_id: str,
        service_id: str,
        config: Any,
        env: Any,
        *,
        reload_trace_id: str | None = None,
    ) -> ReloadAggregateResult:
        """仅对指定租户 (agent_id, service_id) 热重载配置。"""
        if reload_trace_id:
            self._last_reload_trace_id = reload_trace_id
        registry = TenantCatalogRegistry.get_instance()
        existing = registry.get(service_id, agent_id)
        log_reload_config_changes(
            logger,
            env=env,
            config=config,
            previous_config=existing.config if existing is not None else None,
            reload_trace_id=reload_trace_id,
            source="TenantAgentPool",
        )
        self._reconcile_reload_env_for_tenant(
            service_id=service_id,
            agent_id=agent_id,
            env=env if isinstance(env, dict) else None,
            reload_trace_id=reload_trace_id,
        )
        stage_env_overrides(env, service_id=service_id, agent_id=agent_id)
        self._upsert_reload_catalog(
            service_id=service_id,
            agent_id=agent_id,
            config=config,
        )

        aggregate = ReloadAggregateResult()
        cache_key = self._build_cache_key(agent_id, service_id)
        agent_manager = await self._agent_wrappers.get(cache_key)
        if agent_manager is None:
            log_agent_config_hot_reload(
                logger,
                reload_trace_id=reload_trace_id,
                phase="cached",
                source="TenantAgentPool",
                agent_id=agent_id,
                service_id=service_id,
                note="Tenant not cached yet, config saved for future creation",
            )
            return aggregate

        try:
            result = await agent_manager.reload_agents_config(
                config, env, reload_trace_id=reload_trace_id
            )
            aggregate.applied += result.applied
            aggregate.deferred += result.deferred
            aggregate.failed.extend(result.failed)
        except Exception:
            logger.exception(
                "[TenantAgentPool] reload_tenant_config failed for key=%s", cache_key
            )
            aggregate.failed.append({"session": str(cache_key), "error": "tenant reload failed"})
        return aggregate

    async def _ensure_agent_manager(
            self,
            agent_id: str,
            service_id: str | None = None,
            *,
            config_base: Any = None,
            env_overrides: Any = None,
    ) -> Any:
        """确保 agent_id + service_id 对应的 AgentManager 实例已创建（线程安全）."""
        request_agent_id = agent_id
        request_service_id = service_id or "default"
        cache_key = self._build_cache_key(agent_id, service_id)
        lock = self._get_lock(cache_key)

        registry = TenantCatalogRegistry.get_instance()
        spec = registry.get(request_service_id, request_agent_id)
        resolved_config = config_base
        resolved_env = env_overrides
        if resolved_config is None and spec is not None:
            resolved_config = spec.config
        if resolved_env is None and spec is not None:
            resolved_env = materialize_sync_env(spec.env)
        # Gateway cold-start without catalog: tip bags (never cross-agent pool globals).
        if resolved_env is None:
            tip = effective_tip(request_service_id, request_agent_id)
            if tip:
                resolved_env = tip

        async with lock:
            agent_manager = await self._agent_wrappers.get(cache_key)
            if agent_manager is not None:
                return agent_manager

            logger.info(
                "[TenantAgentPool] 创建新 AgentManager 实例: agent_id=%s, service_id=%s",
                agent_id,
                service_id,
            )

            try:
                agent_dir_path = get_multi_tenant_user_workspace_dir(
                    request_service_id, request_agent_id
                )
                if agent_dir_path is None:
                    raise ValueError(
                        f"invalid tenant workspace: agent_id={agent_id!r}, service_id={service_id!r}"
                    )

                import os
                # AGENT_RUNTIME: stable string instance id (legacy "aid_sid" form).
                # Pool isolation uses tuple cache_key; do not pass the tuple as agent_id.
                # Disk / env tip bags still use request_agent_id via env_agent_id.
                agent_runtime = os.getenv("AGENT_RUNTIME", "").strip()
                manager_agent_id = (
                    f"{agent_id}_{service_id}" if agent_runtime else request_agent_id
                )

                from jiuwenclaw.agentserver.agent_manager import AgentManager
                agent_manager = AgentManager(
                    agent_id=manager_agent_id,
                    service_id=request_service_id,
                    user_workspace_dir=agent_dir_path,
                    config_base=resolved_config,
                    env_overrides=resolved_env,
                    last_reload_trace_id=self._last_reload_trace_id,
                    env_agent_id=request_agent_id,
                    env_service_id=request_service_id,
                )

                if resolved_config is not None or resolved_env:
                    log_agent_config_hot_reload(
                        logger,
                        reload_trace_id=self._last_reload_trace_id,
                        phase="cached_apply",
                        source="TenantAgentPool",
                        agent_id=agent_id,
                        service_id=service_id,
                        has_config=resolved_config is not None,
                    )

                await self._agent_wrappers.put(cache_key, agent_manager)

                async with self._global_lock:
                    active_keys = await self._agent_wrappers.keys()
                    stale_locks = [k for k in self._locks if k not in active_keys]
                    for stale_key in stale_locks:
                        del self._locks[stale_key]
                        self._lock_loops.pop(stale_key, None)

                logger.info(
                    "[TenantAgentPool] AgentManager 实例创建完成: agent_id=%s, service_id=%s",
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
        cache_key = self._build_cache_key(agent_id, service_id)
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._agent_wrappers.get(cache_key),
                loop
            )
            return future.result(timeout=1)
        except Exception:
            return None

    @staticmethod
    def _build_cache_key(agent_id: str, service_id: str | None) -> tuple[str, str | None]:
        """Tenant pool key as a tuple to avoid delimiter collisions.

        ``f"{agent_id}_{service_id}"`` would collide for ``(a_b, c)`` vs ``(a, b_c)``.
        """
        return (agent_id, service_id)

    async def _evict_manager_cache(self, agent_id: str, service_id: str) -> None:
        cache_key = self._build_cache_key(agent_id, service_id)
        agent_manager = await self._agent_wrappers.get(cache_key)
        if agent_manager is not None:
            try:
                await agent_manager.cleanup()
            except Exception as exc:
                logger.warning(
                    "[TenantAgentPool] cleanup before evict failed for %s: %s",
                    cache_key,
                    exc,
                )
        await self._agent_wrappers.remove(cache_key)
        self._locks.pop(cache_key, None)
        self._lock_loops.pop(cache_key, None)

        try:
            from jiuwenclaw.agentserver.extensions.rail_manager import RailManagerPool

            RailManagerPool.remove(service_id, agent_id)
        except Exception as exc:
            logger.warning(
                "[TenantAgentPool] RailManagerPool.remove failed for %s: %s",
                cache_key,
                exc,
            )

        try:
            from jiuwenclaw.agentserver.tools.deepresearch_task_manager import (
                DeepResearchTaskManagerPool,
            )

            await DeepResearchTaskManagerPool.remove(service_id, agent_id)
        except Exception as exc:
            logger.warning(
                "[TenantAgentPool] DeepResearchTaskManagerPool.remove failed for %s: %s",
                cache_key,
                exc,
            )

        try:
            from jiuwenclaw.agentserver.cron_local_runtime import AgentCronRegistry

            await AgentCronRegistry.remove(service_id, agent_id)
        except Exception as exc:
            logger.warning(
                "[TenantAgentPool] AgentCronRegistry.remove failed for %s: %s",
                cache_key,
                exc,
            )

    async def warmup_tenant(
        self,
        agent_id: str,
        service_id: str,
        *,
        channel_id: str = "officeclaw",
    ) -> dict[str, Any]:
        """Optional smoke create/destroy for a tenant (not used by sync_agents_configs).

        Full DeepAgent cold-start per catalog agent was too slow for sync; chat creates
        the real session on demand. Kept for manual/debug callers.
        """
        warmup_session = "__warmup__"
        try:
            agent_manager = await self._ensure_agent_manager(agent_id, service_id)
            await agent_manager.get_agent(
                channel_id=channel_id,
                mode="agent",
                session_id=warmup_session,
            )
            await agent_manager.cleanup_session(channel_id, "agent", warmup_session)
            return {"ok": True, "error": None}
        except Exception as exc:
            logger.exception(
                "[TenantAgentPool] warmup failed: agent_id=%s service_id=%s",
                agent_id,
                service_id,
            )
            return {"ok": False, "error": str(exc)}

    async def sync_agents_configs(self, params: dict) -> dict[str, Any]:
        """Apply sync_agents_configs catalog revision for one service_id."""
        async with self._sync_lock:
            validated = validate_sync_payload(params)
            revision = validated["revision"]
            service_id = validated["service_id"]
            agents_payload = validated["agents"]
            shared_env = validated.get("shared_env")
            models_payload = validated.get("models")
            if shared_env is not None:
                logger.info(
                    "[TenantAgentPool] sync_agents_configs shared_env keys=%d service_id=%s",
                    len(shared_env),
                    service_id,
                )

            if models_payload is not None:
                self._build_service_model_cache(service_id, models_payload)
            else:
                self._service_model_cache.pop(service_id, None)

            registry = TenantCatalogRegistry.get_instance()

            if self._last_sync_revision.get(service_id) == revision:
                agent_ids = registry.list_ids(service_id=service_id)
                return {
                    "revision": revision,
                    "service_id": service_id,
                    "agents": [
                        build_agent_result(
                            AgentSyncResultItem(
                                agent_id=aid,
                                action="unchanged",
                                ok=True,
                                warmup={"ok": True, "error": None, "skipped": True},
                            )
                        )
                        for aid in agent_ids
                    ],
                }

            incoming_specs: dict[str, Any] = {}
            for entry in agents_payload:
                agent_id = entry["agent_id"]
                spec = build_agent_spec(
                    service_id=service_id,
                    agent_id=agent_id,
                    config=entry["config"],
                    env=entry["env"],
                    runtime=entry["runtime"],
                    revision=revision,
                )
                incoming_specs[agent_id] = spec

            current_ids = set(registry.list_ids(service_id=service_id))
            incoming_ids = set(incoming_specs)
            removed_ids = current_ids - incoming_ids
            added_ids = incoming_ids - current_ids

            results: list[dict[str, Any]] = []
            all_ok = True

            for agent_id in sorted(removed_ids):
                try:
                    registry.remove(service_id, agent_id)
                    clear_agent_env_ns(service_id, agent_id)
                    await self._evict_manager_cache(agent_id, service_id)
                    results.append(
                        build_agent_result(
                            AgentSyncResultItem(
                                agent_id=agent_id,
                                action="removed",
                                ok=True,
                            )
                        )
                    )
                except Exception as exc:
                    all_ok = False
                    results.append(
                        build_agent_result(
                            AgentSyncResultItem(
                                agent_id=agent_id,
                                action="removed",
                                ok=False,
                                error=str(exc),
                            )
                        )
                    )

            async def _sync_one_agent(
                agent_id: str, spec: Any, action: str,
            ) -> dict[str, Any]:
                # Apply side effects first; commit catalog only on full success so a
                # failed attempt keeps the previous content_hash and identical retries
                # re-run replace_active_env / apply_sync_config.
                materialized_env = materialize_sync_env(spec.env)
                agent_ok = True
                agent_error: str | None = None
                reload_payload: dict[str, Any] | None = None
                warmup_payload: dict[str, Any] | None = None

                try:
                    replace_active_env(
                        materialized_env,
                        service_id=service_id,
                        agent_id=agent_id,
                        clear_staged=True,
                    )
                    agent_manager = await self._ensure_agent_manager(
                        agent_id,
                        service_id,
                        config_base=spec.config,
                        env_overrides=materialized_env,
                    )
                    reload_result = await agent_manager.apply_sync_config(
                        spec.config,
                        materialized_env,
                    )
                    # Baseline keys absent from raw agents[].env -> this tip.
                    # reserved includes null keys so sync deletes are not resurrected.
                    apply_process_baseline_gaps(
                        service_id,
                        agent_id,
                        reserved_keys={str(k) for k in spec.env},
                    )
                    agent_manager._latest_env_overrides = dict(
                        effective_tip(service_id, agent_id)
                    )
                    reload_payload = {
                        "applied": reload_result.applied,
                        "deferred": reload_result.deferred,
                        "failed": reload_result.failed,
                    }
                    if reload_result.failed:
                        agent_ok = False
                        agent_error = "reload failed for one or more sessions"
                    # Skip DeepAgent warmup create/destroy: it dominated sync latency
                    # (~2.5s x N agents) and did not reuse into real chat sessions.
                    warmup_payload = {"ok": True, "error": None, "skipped": True}
                    if agent_ok:
                        registry.upsert(spec)
                except Exception as exc:
                    logger.exception(
                        "[TenantAgentPool] sync agent failed: service_id=%s agent_id=%s",
                        service_id,
                        agent_id,
                    )
                    agent_ok = False
                    agent_error = str(exc)

                if not agent_ok:
                    if action == "added":
                        try:
                            await self._evict_manager_cache(agent_id, service_id)
                        except Exception:
                            logger.debug(
                                "[TenantAgentPool] evict after failed add skipped: "
                                "service_id=%s agent_id=%s",
                                service_id,
                                agent_id,
                                exc_info=True,
                            )
                return build_agent_result(
                    AgentSyncResultItem(
                        agent_id=agent_id,
                        action=action,
                        ok=agent_ok,
                        error=agent_error,
                        warmup=warmup_payload,
                        reload=reload_payload,
                    )
                )

            incoming_task_keys: list[tuple[str, str]] = []
            for agent_id in sorted(incoming_ids):
                spec = incoming_specs[agent_id]
                existing = registry.get(service_id, agent_id)
                if agent_id in added_ids:
                    action = "added"
                elif existing is not None and existing.content_hash == spec.content_hash:
                    action = "unchanged"
                else:
                    action = "updated"
                incoming_task_keys.append((agent_id, action))

            gather_coros = [
                _sync_one_agent(aid, incoming_specs[aid], action)
                for aid, action in incoming_task_keys
                if action != "unchanged"
            ]
            gather_results = await asyncio.gather(*gather_coros, return_exceptions=True)

            gather_idx = 0
            for agent_id, action in incoming_task_keys:
                if action == "unchanged":
                    results.append(
                        build_agent_result(
                            AgentSyncResultItem(
                                agent_id=agent_id,
                                action=action,
                                ok=True,
                                warmup={"ok": True, "error": None, "skipped": True},
                            )
                        )
                    )
                    continue

                raw = gather_results[gather_idx]
                gather_idx += 1
                if isinstance(raw, BaseException):
                    all_ok = False
                    results.append(
                        build_agent_result(
                            AgentSyncResultItem(
                                agent_id=agent_id,
                                action=action,
                                ok=False,
                                error=str(raw),
                            )
                        )
                    )
                else:
                    if not raw.get("ok", True):
                        all_ok = False
                    results.append(raw)

            if all_ok:
                self._last_sync_revision[service_id] = revision

            return {
                "revision": revision,
                "service_id": service_id,
                "agents": results,
            }

    # Web control-plane evolution RPCs that still construct a full agent (LLM client).
    # Disk-only methods (archives/rollback) skip create_instance and do not need this.
    _LLM_CONTROL_EVOLUTION_METHODS: frozenset[str] = frozenset(
        {
            "skills.evolution.rebuild",
            "skills.evolution.status",
            "skills.evolution.get",
            "skills.evolution.save",
        }
    )
    _PREFERRED_CONTROL_AGENT_IDS: tuple[str, ...] = ("office", "jiuwenclaw", "assistant")

    @staticmethod
    def _tip_has_api_base(service_id: str, agent_id: str) -> bool:
        tip = effective_tip(service_id, agent_id) or {}
        return bool(str(tip.get("API_BASE") or "").strip())

    @classmethod
    def resolve_control_rpc_tenant(
        cls,
        request: AgentRequest,
        agent_id: str,
        service_id: str,
    ) -> tuple[str, str]:
        """Remap web evolution RPCs off default tip when it lacks API_BASE.

        ``skills.evolution.rebuild`` (and sibling control RPCs) historically arrive on
        ``channel=web`` without ``agent_id``. That resolves to ``default/default``, whose
        tip often has ``MODEL_NAME`` but omits ``API_BASE`` (credentials live on catalog
        agents such as ``office``). Remount to a credentialed catalog tenant.
        """
        if agent_id != "default" or service_id != "default":
            return agent_id, service_id
        if getattr(request, "channel_id", None) != "web":
            return agent_id, service_id
        req_method = getattr(request, "req_method", None)
        method = getattr(req_method, "value", req_method)
        if not isinstance(method, str) or method not in cls._LLM_CONTROL_EVOLUTION_METHODS:
            return agent_id, service_id
        if cls._tip_has_api_base(service_id, agent_id):
            return agent_id, service_id

        tip = effective_tip(service_id, agent_id) or {}
        desired_model = str(tip.get("MODEL_NAME") or "").strip()
        registry = TenantCatalogRegistry.get_instance()
        catalog_ids = list(registry.list_ids(service_id=service_id))

        candidates: list[str] = []
        for preferred in cls._PREFERRED_CONTROL_AGENT_IDS:
            if preferred in catalog_ids and preferred not in candidates:
                candidates.append(preferred)
        if desired_model:
            for cid in catalog_ids:
                if cid in candidates:
                    continue
                other = effective_tip(service_id, cid) or {}
                if str(other.get("MODEL_NAME") or "").strip() == desired_model:
                    candidates.append(cid)
        for cid in catalog_ids:
            if cid not in candidates:
                candidates.append(cid)

        for cid in candidates:
            if cls._tip_has_api_base(service_id, cid):
                logger.info(
                    "[TenantAgentPool] remapped control RPC tenant default/default -> %s/%s "
                    "(method=%s reason=missing_API_BASE)",
                    service_id,
                    cid,
                    method,
                )
                return cid, service_id
        return agent_id, service_id

    @staticmethod
    def require_officeclaw_agent(request: AgentRequest) -> AgentResponse | None:
        """Allow legacy default/default; require catalog membership for named tenants."""
        if request.channel_id != "officeclaw":
            return None

        raw_agent = getattr(request, "agent_id", None)
        raw_service = getattr(request, "service_id", None)
        try:
            agent_id = normalize_env_ns_id(
                str(raw_agent).strip() if raw_agent is not None else "",
                default="default",
            )
            service_id = normalize_env_ns_id(
                str(raw_service).strip() if raw_service is not None else "",
                default="default",
            )
        except EnvNsIdError as exc:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "invalid_tenant_id"},
            )

        # Backward compatibility: relay-claw currently runs one sidecar per agent
        # and does not send tenant IDs. Process isolation makes default/default safe.
        if agent_id == "default" and service_id == "default":
            return None

        registry = TenantCatalogRegistry.get_instance()
        if not registry.contains(service_id, agent_id):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": f"tenant not registered: service_id={service_id!r} agent_id={agent_id!r}",
                    "code": "tenant_not_registered",
                },
            )
        return None

    async def _refresh_agent_manager_cache(
            self,
            cache_key: Hashable,
            agent_manager: Any,
    ) -> None:
        """请求结束后刷新 LRU 时间戳，避免长任务执行期间 AgentManager 被 TTL 淘汰."""
        if agent_manager is None:
            return
        try:
            refreshed = await self._agent_wrappers.touch_if_same(cache_key, agent_manager)
            if not refreshed:
                logger.debug(
                    "[TenantAgentPool] skip cache refresh for key=%s: entry replaced or expired",
                    cache_key,
                )
        except Exception:
            logger.exception(
                "[TenantAgentPool] failed to refresh AgentManager cache for key=%s",
                cache_key,
            )

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """处理非流式请求."""
        guard = self.require_officeclaw_agent(request)
        if guard is not None:
            return guard
        agent_id, service_id = self.extract_ids(request)
        agent_id, service_id = self.resolve_control_rpc_tenant(request, agent_id, service_id)
        cache_key = self._build_cache_key(agent_id, service_id)
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        try:
            return await agent_manager.process_message(request)
        finally:
            await self._refresh_agent_manager_cache(cache_key, agent_manager)

    async def process_message_stream(
            self, request: AgentRequest
    ):
        """处理流式请求."""
        guard = self.require_officeclaw_agent(request)
        if guard is not None:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": guard.payload.get("error") if guard.payload else "officeclaw guard failed",
                    "code": guard.payload.get("code") if guard.payload else None,
                },
                is_complete=True,
            )
            return
        agent_id, service_id = self.extract_ids(request)
        agent_id, service_id = self.resolve_control_rpc_tenant(request, agent_id, service_id)
        cache_key = self._build_cache_key(agent_id, service_id)
        agent_manager = await self._ensure_agent_manager(agent_id, service_id)
        params = request.params if isinstance(request.params, dict) else {}
        mode_full = params.get("mode", "agent.plan")
        mode = str(mode_full).split(".")[0] if mode_full else "agent"
        workspace_dir = params.get("workspace_dir")
        session_id = getattr(request, "session_id", None)
        agent = await agent_manager.get_agent(
            channel_id=request.channel_id,
            mode=mode,
            workspace_dir=workspace_dir,
            session_id=session_id,
            request=request,
        )
        tenant_tokens = None
        mem_token = None
        if agent is not None:
            tenant_tokens, mem_token = agent._bind_tenant_request_context()  # pylint: disable=protected-access
        try:
            async for chunk in agent_manager.process_message_stream(request):
                yield chunk
        finally:
            if agent is not None and tenant_tokens is not None:
                agent._reset_tenant_request_context(tenant_tokens, mem_token)  # pylint: disable=protected-access
            await self._refresh_agent_manager_cache(cache_key, agent_manager)

    @staticmethod
    def normalize_tenant_id(
        value: str | None,
        *,
        default: str = "default",
    ) -> str:
        if value is None:
            return default
        stripped = str(value).strip()
        if not stripped:
            return default
        return normalize_env_ns_id(stripped, default=default)

    @staticmethod
    def extract_ids(request: AgentRequest):
        """从请求中提取 agent_id 和 service_id."""
        agent_id = getattr(request, "agent_id", None)
        service_id = getattr(request, "service_id", None)

        if request.channel_id == "acp":
            return "acp", "global_acp"

        if request.channel_id == "officeclaw":
            aid = TenantAgentPool.normalize_tenant_id(agent_id)
            sid = TenantAgentPool.normalize_tenant_id(service_id)
            return aid, sid

        agent_id = TenantAgentPool.normalize_tenant_id(agent_id)
        service_id = TenantAgentPool.normalize_tenant_id(service_id)
        return agent_id, service_id

    async def reload_agent_config(
            self,
            agent_id: str,
            config_base: Any = None,
            env_overrides: dict | None = None,
            *,
            service_id: str | None = None,
    ) -> None:
        """重新加载指定租户的 Agent 配置."""
        aid = self.normalize_tenant_id(agent_id)
        if aid == "acp":
            sid = "global_acp"
        else:
            sid = self.normalize_tenant_id(service_id)
        agent_manager = await self._ensure_agent_manager(aid, sid)
        channel_id = "acp" if aid == "acp" else "default"
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

    def iter_agent_managers_nowait(self) -> list[Any]:
        """Return cached ``AgentManager`` instances without creating new ones."""
        return filter_cached_agent_managers(self._agent_wrappers.snapshot_values_nowait())

    def collect_runtime_tools_catalog_nowait(self) -> dict[str, dict[str, str]]:
        """Union tool catalogs from all initialized JiuWenClaw instances."""
        from jiuwenclaw.agentserver.tool_catalog import collect_tools_catalog_from_claws

        claws: list[Any] = []
        for agent_manager in self.iter_agent_managers_nowait():
            claws.extend(agent_manager.iter_jiuwenclaw_instances())
        return collect_tools_catalog_from_claws(claws)
