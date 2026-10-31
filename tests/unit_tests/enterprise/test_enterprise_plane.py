# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业平面组装与 TenantStrategy 分流."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.enterprise import (
    get_enterprise_plane,
    reset_enterprise_plane,
    resolve_tenant_strategy,
    try_build_enterprise_plane,
)
from jiuwenswarm.enterprise.tenant.multi_tenant_strategy import MultiTenantStrategy
from jiuwenswarm.enterprise.tenant.single_tenant_strategy import SingleTenantStrategy
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_manager import AgentManager


@pytest.fixture(autouse=True)
def _reset_plane():
    reset_enterprise_plane()
    yield
    reset_enterprise_plane()


def test_try_build_returns_none_for_personal(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    assert try_build_enterprise_plane() is None
    assert get_enterprise_plane() is None


def test_try_build_assembles_multi_for_enterprise(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    plane = try_build_enterprise_plane()
    assert plane is not None
    assert get_enterprise_plane() is plane
    assert isinstance(plane.tenant, MultiTenantStrategy)


def test_resolve_tenant_strategy_personal(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    strategy = resolve_tenant_strategy()
    assert isinstance(strategy, SingleTenantStrategy)


@pytest.mark.asyncio
async def test_single_strategy_ignores_triple(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    strategy = SingleTenantStrategy(AgentManager())
    req = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "u"}},
    )
    assert strategy.extract_tenant_key(req) == build_tenant_key()
    assert await strategy.resolve_manager(req) is strategy.get_default_manager()


@pytest.mark.asyncio
async def test_multi_strategy_splits_by_triple(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool

    TenantAgentPool.reset_instance()
    strategy = MultiTenantStrategy(TenantAgentPool.get_instance())
    strategy.adopt_default_manager(AgentManager())

    req_a = AgentRequest(
        request_id="a",
        channel_id="web",
        session_id="s-a",
        user_id="alice",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "alice"}},
    )
    req_b = AgentRequest(
        request_id="b",
        channel_id="web",
        session_id="s-b",
        user_id="bob",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "bob"}},
    )
    key_a = strategy.extract_tenant_key(req_a)
    key_b = strategy.extract_tenant_key(req_b)
    assert key_a != key_b
    assert key_a == build_tenant_key("g", "b", "alice")

    manager_a = await strategy.resolve_manager(req_a)
    manager_b = await strategy.resolve_manager(req_b)
    assert manager_a is not manager_b

    await strategy.cleanup()
    TenantAgentPool.reset_instance()


@pytest.mark.asyncio
async def test_agent_runtime_personal_uses_single(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    runtime = AgentRuntime(initializer=AsyncMock())
    assert isinstance(runtime.tenant_strategy, SingleTenantStrategy)
