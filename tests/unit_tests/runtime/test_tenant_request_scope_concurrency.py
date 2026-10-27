# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: concurrent tenant_scope 各持局部 manager，互不覆盖."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime import AgentRuntime


class _NamedManager:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_concurrent_tenant_scopes_yield_isolated_managers(
    monkeypatch,
) -> None:
    default = _NamedManager("default")
    manager_a = _NamedManager("tenant-a")
    manager_b = _NamedManager("tenant-b")
    by_key = {"tenant-a": manager_a, "tenant-b": manager_b}

    @asynccontextmanager
    async def tenant_scope(request: AgentRequest):
        yield by_key[request.metadata["tenant_key"]]

    pool = SimpleNamespace(
        put_nowait=lambda _key, manager: manager,
        get_manager_nowait=lambda _key=None: default,
        tenant_scope=tenant_scope,
        iter_agent_managers_nowait=lambda: [default],
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.service.TenantAgentPool",
        lambda: pool,
    )

    runtime = AgentRuntime(agent_manager=default, initializer=AsyncMock())
    seen: dict[str, list[str]] = {"a": [], "b": []}

    async def _run(label: str, tenant_key: str, expected: _NamedManager) -> None:
        request = AgentRequest(
            request_id=f"req-{label}",
            channel_id="web",
            session_id=f"sess-{label}",
            metadata={"tenant_key": tenant_key},
        )
        async with runtime._tenant_pool.tenant_scope(request) as manager:
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
@pytest.mark.parametrize("enterprise", [False, True])
async def test_owned_pool_preserves_edition_routing_and_runtime_isolation(monkeypatch, enterprise):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise" if enterprise else "personal")
    first_default = SimpleNamespace(cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock())
    second_default = SimpleNamespace(cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock())
    first = AgentRuntime(agent_manager=first_default, initializer=AsyncMock())
    second = AgentRuntime(agent_manager=second_default, initializer=AsyncMock())
    requests = [AgentRequest(
        request_id=f"request-{user}", channel_id="web", session_id=f"session-{user}",
        metadata={"user_id": user, "routing": {"group_id": "group", "bot_id": "bot"}},
    ) for user in ("alice", "bob")]
    managers = []
    for request in requests:
        manager = SimpleNamespace(cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock())
        managers.append(first.tenant_pool.put_nowait(first.tenant_pool.extract_tenant_key(request), manager))
    assert (managers[0] is managers[1]) is (not enterprise)
    for request, expected in zip(requests, managers):
        async with first.tenant_pool.tenant_scope(request) as actual:
            assert actual is expected
            assert await first.resolve_manager(request) is expected
    await second.close()
    for request, expected in zip(requests, managers):
        assert await first.resolve_manager(request) is expected
        expected.cleanup.assert_not_awaited()
    second_default.cleanup.assert_awaited_once()
    await first.close()
    for expected in {id(manager): manager for manager in managers}.values():
        expected.cleanup.assert_awaited_once()
