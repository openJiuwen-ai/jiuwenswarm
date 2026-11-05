# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 MultiTenantStrategy：委托 TenantAgentPool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Iterable

from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.runtime.agent_manager import AgentManager

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest


class MultiTenantStrategy:
    """企业版：按 tenant_key 分槽；bind 时写入 TenantContext + 企业工作区."""

    def __init__(self, pool: TenantAgentPool | None = None) -> None:
        self._pool = pool or TenantAgentPool.get_instance()

    @property
    def pool(self) -> TenantAgentPool:
        return self._pool

    @staticmethod
    def extract_tenant_key(request: AgentRequest | None) -> str:
        if request is None:
            return build_tenant_key()
        # 企业实现：始终按三元组分槽（不回落 is_enterprise 闸门）。
        from jiuwenswarm.enterprise.tenant.tenant_identity import extract_triple

        group_id, bot_id, user_id = extract_triple(request)
        return build_tenant_key(group_id, bot_id, user_id)

    async def resolve_manager(
        self, request: AgentRequest | None = None
    ) -> AgentManager:
        if request is None:
            return self.get_default_manager()
        tenant_key = self.extract_tenant_key(request)
        return await self._pool.get_agent_manager(tenant_key)

    def get_default_manager(self) -> AgentManager:
        manager = self._pool.get_manager_nowait(build_tenant_key())
        if manager is None:
            raise RuntimeError(
                "default tenant AgentManager is missing; "
                "MultiTenantStrategy must adopt_default_manager before use"
            )
        return manager

    def adopt_default_manager(self, manager: AgentManager) -> AgentManager:
        return self._pool.put_nowait(build_tenant_key(), manager)

    @asynccontextmanager
    async def bind_request(
        self, request: AgentRequest, manager: AgentManager
    ) -> AsyncIterator[None]:
        tenant_key = self.extract_tenant_key(request)
        ctx = TenantContext.bind(tenant_key)
        try:
            try:
                yield
            finally:
                await self._pool.touch_manager_if_same(tenant_key, manager)
        finally:
            ctx.reset()

    def iter_managers_for_host_event(self) -> Iterable[AgentManager]:
        return self._pool.iter_agent_managers_nowait()

    async def cleanup(self) -> None:
        await self._pool.cleanup()

    @asynccontextmanager
    async def scoped_manager(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentManager]:
        # 与 Pool.tenant_scope 对齐：bind + ensure + touch。
        async with self._pool.tenant_scope(request) as manager:
            yield manager
