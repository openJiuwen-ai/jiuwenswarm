# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: concurrent scoped_manager 各持局部 manager，互不覆盖."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.enterprise.tenant.single_tenant_strategy import SingleTenantStrategy
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_manager import AgentManager


class _NamedManager:
    def __init__(self, name: str) -> None:
        self.name = name

    async def cleanup(self) -> None:
        return None


class _FakeMultiStrategy:
    """测试用：按 metadata.tenant_key 分槽，不依赖真实 Pool."""

    def __init__(self, default: _NamedManager, by_key: dict[str, _NamedManager]) -> None:
        self._default = default
        self._by_key = by_key

    def extract_tenant_key(self, request: AgentRequest | None) -> str:
        if request is None:
            return "default"
        meta = request.metadata if isinstance(request.metadata, dict) else {}
        return str(meta.get("tenant_key") or "default")

    async def resolve_manager(self, request: AgentRequest | None = None):
        if request is None:
            return self._default
        return self._by_key[self.extract_tenant_key(request)]

    def get_default_manager(self):
        return self._default

    def adopt_default_manager(self, manager):
        return self._default

    @asynccontextmanager
    async def bind_request(self, request, manager) -> AsyncIterator[None]:
        yield

    def iter_managers_for_host_event(self):
        return [self._default, *self._by_key.values()]

    async def cleanup(self) -> None:
        return None

    @asynccontextmanager
    async def scoped_manager(self, request: AgentRequest) -> AsyncIterator[_NamedManager]:
        manager = await self.resolve_manager(request)
        async with self.bind_request(request, manager):
            yield manager


@pytest.mark.asyncio
async def test_concurrent_tenant_scopes_yield_isolated_managers() -> None:
    default = _NamedManager("default")
    manager_a = _NamedManager("tenant-a")
    manager_b = _NamedManager("tenant-b")
    strategy = _FakeMultiStrategy(
        default,
        {"tenant-a": manager_a, "tenant-b": manager_b},
    )

    runtime = AgentRuntime(
        agent_manager=default,  # type: ignore[arg-type]
        tenant_strategy=strategy,  # type: ignore[arg-type]
        initializer=AsyncMock(),
    )
    seen: dict[str, list[str]] = {"a": [], "b": []}

    async def _run(label: str, tenant_key: str, expected: _NamedManager) -> None:
        request = AgentRequest(
            request_id=f"req-{label}",
            channel_id="web",
            session_id=f"sess-{label}",
            metadata={"tenant_key": tenant_key},
        )
        async with runtime.tenant_strategy.scoped_manager(request) as manager:
            assert manager is expected
            # 默认槽属性始终指向默认 Manager，不被并发请求改写。
            assert runtime.agent_manager is default
            await asyncio.sleep(0)
            seen[label].append(manager.name)
            assert manager is expected
            assert runtime.agent_manager is default

    await asyncio.gather(
        _run("a", "tenant-a", manager_a),
        _run("b", "tenant-b", manager_b),
    )

    assert seen["a"] == ["tenant-a"]
    assert seen["b"] == ["tenant-b"]
    assert runtime.agent_manager is default


@pytest.mark.asyncio
async def test_personal_runtime_uses_single_strategy_without_pool(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    from jiuwenswarm.enterprise import reset_enterprise_plane

    reset_enterprise_plane()
    manager = AgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    assert isinstance(runtime.tenant_strategy, SingleTenantStrategy)
    assert runtime.agent_manager is manager
    with pytest.raises(Exception):
        _ = runtime.tenant_pool
