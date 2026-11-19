# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""handlers 经 bind_request_tenant 拿到本请求租户 Manager，而非默认槽."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.context import (
    AgentServerServices,
    RequestContext,
    bind_request_tenant,
)
from jiuwenswarm.server.dispatch import try_dispatch
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


class _FakePool:
    def extract_tenant_key(self, request: AgentRequest) -> str:
        from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool

        return TenantAgentPool.extract_tenant_key(request)


class _FakeRuntime:
    def __init__(self, default_manager: object) -> None:
        self._tenant_pool = _FakePool()
        self.agent_manager = default_manager
        self.managers: dict[str, object] = {"default": default_manager}

    async def resolve_manager(self, request: AgentRequest | None = None):
        key = self._tenant_pool.extract_tenant_key(request) if request else "default"
        if key not in self.managers:
            self.managers[key] = SimpleNamespace(tenant_key=key)
        return self.managers[key]


def _services_with_runtime(runtime: _FakeRuntime, default_manager: object) -> AgentServerServices:
    app = AppServices()
    app.agent_manager = default_manager
    app.runtime = runtime
    return AgentServerServices(app)


@pytest.mark.asyncio
async def test_bind_request_tenant_scopes_manager_and_restores(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    default_manager = SimpleNamespace(tenant_key="default")
    runtime = _FakeRuntime(default_manager)
    services = _services_with_runtime(runtime, default_manager)

    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=ReqMethod.SKILLS_LIST,
        metadata={"routing": {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}},
        user_id="u1",
    )
    ctx = RequestContext(
        request=request,
        sink=AsyncMock(),
        connection_id="c1",
        services=services,
    )
    expected_key = build_tenant_key("g1", "b1", "u1")

    assert services.agent_manager is default_manager
    async with bind_request_tenant(ctx):
        assert services.agent_manager.tenant_key == expected_key
        assert TenantContext.current_tenant_key() == expected_key
    assert services.agent_manager is default_manager
    assert TenantContext.current_tenant_key() is None


@pytest.mark.asyncio
async def test_try_dispatch_uses_tenant_manager(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    seen: list[object] = []

    async def fake_skills(ctx: RequestContext) -> None:
        seen.append(ctx.services.agent_manager)

    from jiuwenswarm.server import dispatch as dispatch_mod

    monkeypatch.setitem(dispatch_mod.HANDLERS, ReqMethod.SKILLS_LIST, fake_skills)

    default_manager = SimpleNamespace(tenant_key="default")
    runtime = _FakeRuntime(default_manager)
    services = _services_with_runtime(runtime, default_manager)

    request = AgentRequest(
        request_id="r2",
        channel_id="web",
        req_method=ReqMethod.SKILLS_LIST,
        metadata={"routing": {"group_id": "g2", "bot_id": "b2", "user_id": "u2"}},
        user_id="u2",
    )
    ctx = RequestContext(
        request=request,
        sink=AsyncMock(),
        connection_id="c2",
        services=services,
    )
    assert await try_dispatch(ctx) is True
    assert len(seen) == 1
    assert seen[0].tenant_key == build_tenant_key("g2", "b2", "u2")
    assert services.agent_manager is default_manager


@pytest.mark.asyncio
async def test_personal_edition_collapses_to_single_manager(monkeypatch):
    """个人版无论请求身份如何，池键与 extract 均落到默认槽."""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool

    default_key = build_tenant_key()
    req_a = AgentRequest(
        request_id="a",
        channel_id="web",
        user_id="alice",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "alice"}},
    )
    req_b = AgentRequest(
        request_id="b",
        channel_id="web",
        user_id="bob",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "bob"}},
    )
    assert TenantAgentPool.extract_tenant_key(req_a) == default_key
    assert TenantAgentPool.extract_tenant_key(req_b) == default_key
    assert TenantAgentPool._pool_key(build_tenant_key("g", "b", "alice")) == default_key
    assert TenantAgentPool._pool_key(build_tenant_key("g", "b", "bob")) == default_key
