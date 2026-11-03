# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TenantStrategy：个人版 Single / 企业版 Multi 的公共端口."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Protocol, runtime_checkable

from jiuwenswarm.server.runtime.agent_manager import AgentManager

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest


@runtime_checkable
class TenantStrategy(Protocol):
    """请求级租户解析与绑定；不含传输、不含 GDB."""

    def extract_tenant_key(self, request: AgentRequest | None) -> str:
        ...

    async def resolve_manager(
        self, request: AgentRequest | None = None
    ) -> AgentManager:
        ...

    def get_default_manager(self) -> AgentManager:
        ...

    def adopt_default_manager(self, manager: AgentManager) -> AgentManager:
        """若默认槽尚未登记，采用传入 Manager（不覆盖已有）."""
        ...

    def bind_request(
        self, request: AgentRequest, manager: AgentManager
    ) -> Any:
        """异步上下文：绑定 TenantContext（若需要）并在退出时清理 / LRU 续约."""
        ...

    def iter_managers_for_host_event(self) -> Iterable[AgentManager]:
        ...

    async def cleanup(self) -> None:
        ...

    @asynccontextmanager
    async def scoped_manager(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentManager]:
        """resolve + bind，供 Runtime stream/invoke 使用."""
        manager = await self.resolve_manager(request)
        async with self.bind_request(request, manager):
            yield manager
