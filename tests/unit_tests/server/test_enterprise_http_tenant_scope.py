"""HTTP handlers use the existing tenant scope even without AgentManager."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.http_invoke import invoke_stream, invoke_unary
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
@pytest.mark.parametrize("edition", ["enterprise", "personal"])
async def test_http_handler_scope_is_bound_and_restored(monkeypatch, tmp_path, invoke, edition):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    request = AgentRequest(
        request_id="r", channel_id="web", user_id="u", session_id="s",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "u"}},
    )
    ctx = SimpleNamespace(request=request, sink=AsyncMock())

    async def handler(_ctx):
        # 企业按请求身份绑 tenant_key；个人版固定默认槽，避免多 Manager。
        if edition == "enterprise":
            expected = utils.build_tenant_key("g", "b", "u")
            assert TenantContext.current_tenant_key() == expected
            assert utils.get_agent_sessions_dir().is_relative_to(tmp_path)
        else:
            assert TenantContext.current_tenant_key() == utils.build_tenant_key()

    with TenantContext.scoped("outer"):
        await invoke(ctx, handler)
        assert TenantContext.current_tenant_key() == "outer"


@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_http_scope_restores_after_error_or_cancellation(monkeypatch, tmp_path, invoke, error):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", user_id="u",
        metadata={"routing": {"group_id": "g", "bot_id": "b"}},
    )
    sink = AsyncMock()

    async def handler(_ctx):
        assert TenantContext.current_tenant_key() == utils.build_tenant_key("g", "b", "u")
        raise error("interrupted")

    with TenantContext.scoped("outer"):
        ctx = SimpleNamespace(request=request, sink=sink)
        if error is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await invoke(ctx, handler)
            sink.send_response.assert_not_awaited()
        else:
            await invoke(ctx, handler)
            assert sink.send_response.await_args.args[0].ok is False
        assert TenantContext.current_tenant_key() == "outer"
    if invoke is invoke_stream:
        sink.close.assert_awaited_once()
