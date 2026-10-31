# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""个人版 SingleTenantStrategy：进程内唯一 Manager."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Iterable

from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key
from jiuwenswarm.server.runtime.agent_manager import AgentManager

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest


class SingleTenantStrategy:
    """个人版：进程内唯一 Manager，忽略三元组，不改企业路径."""

    def __init__(self, manager: AgentManager | None = None) -> None:
        self._manager = manager or AgentManager()

    @staticmethod
    def extract_tenant_key(request: AgentRequest | None) -> str:
        return build_tenant_key()

    async def resolve_manager(
        self, request: AgentRequest | None = None
    ) -> AgentManager:
        return self._manager

    def get_default_manager(self) -> AgentManager:
        return self._manager

    def adopt_default_manager(self, manager: AgentManager) -> AgentManager:
        # 已有默认实例时不覆盖，与 Pool.put_nowait 语义一致。
        return self._manager

    @asynccontextmanager
    async def bind_request(
        self, request: AgentRequest, manager: AgentManager
    ) -> AsyncIterator[None]:
        # 个人版：不绑 TenantContext 路径 CV，保持 develop 落盘布局。
        yield

    def iter_managers_for_host_event(self) -> Iterable[AgentManager]:
        return (self._manager,)

    async def cleanup(self) -> None:
        cleanup = getattr(self._manager, "cleanup", None)
        if callable(cleanup):
            await cleanup()

    @asynccontextmanager
    async def scoped_manager(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentManager]:
        manager = await self.resolve_manager(request)
        async with self.bind_request(request, manager):
            yield manager
