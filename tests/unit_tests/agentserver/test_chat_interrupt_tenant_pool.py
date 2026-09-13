# coding: utf-8
"""chat.interrupt 对 officeclaw/租户池请求必须路由到持有该 session 的租户 agent。

问题（#3755 现象 1）：officeclaw 流式 agent 注册在 TenantAgentPool 的租户
AgentManager 里，不在 ctx.services.agent_manager 中；cancel 请求又不带
project_dir，按 (channel, mode, project) 缓存键必然 miss。旧逻辑直接回
"no existing agent" 假响应——adapter.process_interrupt 永不执行，round
不终止、interaction output lease 不释放，后续消息全部被 ACK-only 丢弃
（"cancel 后发继续执行无反应，直接已完成"）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod


def _tenant_cancel_request(*, session_id="officeclaw_sess-1", intent="cancel"):
    return AgentRequest(
        request_id="interrupt-tenant-1",
        channel_id="officeclaw",
        session_id=session_id,
        agent_id="office",
        req_method=ReqMethod.CHAT_CANCEL,
        # 注意：无 project_dir（真实 cancel 请求形态）
        params={"intent": intent, "request_id": "run-1", "mode": "agent.plan"},
        is_stream=False,
    )


def _tenant_agent(*, owns_session=None):
    """构造一个 JiuWenSwarm stub，其根 adapter 的 _session_adapters 含 owns_session。"""
    agent = MagicMock()
    agent.process_message = AsyncMock(
        return_value=AgentResponse(
            request_id="interrupt-tenant-1",
            channel_id="officeclaw",
            ok=True,
            payload={
                "event_type": "chat.interrupt_result",
                "intent": "cancel",
                "success": True,
                "message": "任务已取消",
            },
        )
    )
    root_adapter = SimpleNamespace(
        _session_adapters={owns_session: MagicMock()} if owns_session else {}
    )
    agent._adapter = root_adapter
    return agent


def _tenant_pool(agent):
    pool = MagicMock()
    pool.extract_ids = MagicMock(return_value=("office", "default", "default"))
    pool.resolve_control_rpc_tenant = MagicMock(
        side_effect=lambda _req, agent_id, service_id: (agent_id, service_id)
    )
    pool.get_agent_manager = AsyncMock(
        return_value=SimpleNamespace(agents={"officeclaw": {"agent::p1": agent}})
    )
    return pool


def _ctx(request, *, tenant_pool=None, agent_nowait=None):
    services = SimpleNamespace(
        agent_manager=SimpleNamespace(
            get_agent_nowait=MagicMock(return_value=agent_nowait),
            get_agent=AsyncMock(return_value=agent_nowait),
            get_client_capabilities=MagicMock(return_value={}),
        ),
        session_stream_tasks={request.session_id or "default": {}},
    )
    if tenant_pool is not None:
        services.tenant_pool = MagicMock(return_value=tenant_pool)
    sink = AsyncMock()
    return SimpleNamespace(request=request, services=services, sink=sink)


@pytest.mark.asyncio
async def test_tenant_pool_cancel_routes_to_session_owned_agent():
    """officeclaw cancel 必须按 session 归属定位租户 agent 并走其 process_message。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel

    agent = _tenant_agent(owns_session="officeclaw_sess-1")
    req = _tenant_cancel_request(session_id="officeclaw_sess-1")
    resp = await _handle_cancel(
        _ctx(req, tenant_pool=_tenant_pool(agent)), allow_create=False, send_response=False
    )

    agent.process_message.assert_awaited_once_with(req)
    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is True


@pytest.mark.asyncio
async def test_tenant_pool_cancel_skips_agent_owning_other_session():
    """其它 session 的 agent 不能被误路由：session 归属不匹配时不得调它的 process_message。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel

    other_agent = _tenant_agent(owns_session="officeclaw_sess-other")
    req = _tenant_cancel_request(session_id="officeclaw_sess-1")
    ctx = _ctx(req, tenant_pool=_tenant_pool(other_agent), agent_nowait=None)
    resp = await _handle_cancel(ctx, allow_create=False, send_response=False)

    other_agent.process_message.assert_not_awaited()
    # 回退到默认路径：无 agent → 假响应（现状行为）
    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is True


@pytest.mark.asyncio
async def test_tenant_pool_cancel_falls_back_when_pool_unavailable():
    """tenant_pool 服务缺失/异常时不得抛出：回退默认逻辑返回假响应。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel

    req = _tenant_cancel_request(session_id="officeclaw_sess-1")
    # ctx.services 无 tenant_pool 属性 → AttributeError 必须被兜住
    ctx = _ctx(req, tenant_pool=None)
    resp = await _handle_cancel(ctx, allow_create=False, send_response=False)

    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is True


@pytest.mark.asyncio
async def test_tenant_pool_cancel_falls_back_when_manager_resolution_raises():
    """租户 manager 解析链路抛异常时回退默认逻辑，不向调用方传播异常。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel

    pool = MagicMock()
    pool.extract_ids = MagicMock(return_value=("office", "default", "default"))
    pool.resolve_control_rpc_tenant = MagicMock(
        side_effect=RuntimeError("tenant store down")
    )
    req = _tenant_cancel_request(session_id="officeclaw_sess-1")
    ctx = _ctx(req, tenant_pool=pool)
    resp = await _handle_cancel(ctx, allow_create=False, send_response=False)

    assert resp.payload["event_type"] == "chat.interrupt_result"
    assert resp.payload["success"] is True


@pytest.mark.asyncio
async def test_non_tenant_pool_cancel_keeps_agent_manager_path():
    """非租户池渠道（web）不受影响：仍走 agent_manager 查找。"""
    from jiuwenswarm.server.handlers.chat import _handle_cancel

    req = AgentRequest(
        request_id="interrupt-web-1",
        channel_id="web",
        session_id="web-sess",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "mode": "agent"},
        is_stream=False,
    )
    agent = MagicMock()
    agent.process_message = AsyncMock(
        return_value=AgentResponse(
            request_id="interrupt-web-1", channel_id="web", ok=True, payload={}
        )
    )
    ctx = _ctx(req, agent_nowait=agent)
    resp = await _handle_cancel(ctx, allow_create=False, send_response=False)

    agent.process_message.assert_awaited_once_with(req)
    assert resp.ok is True


def test_find_tenant_pool_agent_owning_session_matches_cached_session():
    from jiuwenswarm.server.handlers.chat import _find_tenant_pool_agent_owning_session

    owner = _tenant_agent(owns_session="sess-a")
    other = _tenant_agent(owns_session="sess-b")
    manager = SimpleNamespace(
        agents={"officeclaw": {"agent::p1": owner, "agent::p2": other}}
    )

    assert _find_tenant_pool_agent_owning_session(manager, "sess-a") is owner
    assert _find_tenant_pool_agent_owning_session(manager, "sess-b") is other
    assert _find_tenant_pool_agent_owning_session(manager, "sess-unknown") is None
    assert _find_tenant_pool_agent_owning_session(manager, None) is None
    assert _find_tenant_pool_agent_owning_session(manager, "") is None
