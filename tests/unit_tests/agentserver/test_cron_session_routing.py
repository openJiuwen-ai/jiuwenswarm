# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scheme A: plain cron_* follow-up on web; interrupt resume finds session owner."""

# pylint: disable=protected-access

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.context import AgentServerServices, RequestContext
from jiuwenswarm.server.handlers import _default
from jiuwenswarm.server.runtime import agent_manager as am
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.transports.sink import WSSink


def _default_ctx(server, request):
    import asyncio as _asyncio

    class _NullWs:
        async def send(self, text):  # noqa: ANN001
            return None

    _ws = _NullWs()
    return RequestContext(
        request=request,
        sink=WSSink(_ws, _asyncio.Lock()),
        connection_id=str(id(_ws)),
        services=AgentServerServices(server),
    )


def test_cron_agent_channel_helpers_cover_followup_and_resume() -> None:
    followup = SimpleNamespace(
        session_id="cron_abc",
        channel_id="web",
        params={"query": "1"},
    )
    resume = SimpleNamespace(
        session_id="cron_abc",
        channel_id="web",
        params={
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "perm-1",
        },
    )
    normal = SimpleNamespace(
        session_id="sess_web",
        channel_id="web",
        params={"query": "hi"},
    )

    assert am._is_cron_session_request(followup) is True
    assert am._is_cron_permission_resume(followup) is False
    assert am._is_interrupt_resume_request(followup) is False
    # Plain follow-up stays on request channel (web), not forced to __cron__.
    assert am._cron_agent_channel_for_request(followup) == "web"
    assert am._cron_permission_resume_channel(followup) == "web"

    assert am._is_interrupt_resume_request(resume) is True
    assert am._is_cron_permission_resume(resume) is True
    assert am._cron_agent_channel_for_request(resume) == "__cron__"

    assert am._is_cron_session_request(normal) is False
    assert am._cron_agent_channel_for_request(normal) == "web"


@pytest.mark.asyncio
async def test_agent_manager_plain_cron_followup_uses_request_channel() -> None:
    """Plain cron_* follow-up must get_agent(web), not reuse __cron__ owner."""
    owned = MagicMock(name="owned_cron")
    owned._jiuwenswarm_agent_channel_id = "__cron__"
    web_agent = MagicMock(name="web_agent")
    manager = AgentManager.__new__(AgentManager)
    manager.get_agent_for_session_nowait = MagicMock(return_value=owned)
    manager.find_agent_for_session_nowait = MagicMock(return_value=owned)
    manager.get_agent = AsyncMock(return_value=web_agent)
    manager._agent_lookup_from_request = MagicMock(return_value=("agent", None, None))

    request = SimpleNamespace(
        session_id="cron_job_x",
        channel_id="web",
        params={"query": "1"},
    )
    selected = await manager._get_agent_for_request(request)
    assert selected is web_agent
    manager.find_agent_for_session_nowait.assert_not_called()
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="agent",
        project_dir=None,
        sub_mode=None,
        request=None,
    )


@pytest.mark.asyncio
async def test_agent_manager_interrupt_resume_reuses_owner() -> None:
    owned = MagicMock(name="owned_cron")
    owned._jiuwenswarm_agent_channel_id = "__cron__"
    manager = AgentManager.__new__(AgentManager)
    manager.get_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_interrupt_resume_nowait = MagicMock(return_value=owned)
    manager.get_agent = AsyncMock(return_value=MagicMock())
    manager._agent_lookup_from_request = MagicMock(return_value=("agent", None, None))

    request = SimpleNamespace(
        session_id="cron_job_x",
        channel_id="web",
        params={
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "perm-1",
        },
    )
    selected = await manager._get_agent_for_request(request)
    assert selected is owned
    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_followup_routes_to_web_channel_agent() -> None:
    """Plain cron_* follow-up uses request channel (web), not __cron__."""
    web_agent = MagicMock(name="web_agent")
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=web_agent)
    manager.wait_for_session_prewarm = AsyncMock()
    manager.get_agent_for_session_nowait = MagicMock(return_value=MagicMock())
    manager.find_agent_for_session_nowait = MagicMock(return_value=MagicMock())
    manager.find_agent_for_interrupt_resume_nowait = MagicMock(return_value=None)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="cron-followup",
        channel_id="web",
        session_id="cron_job1_run2",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "1"},
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"mode": "agent", "work_mode": "work"},
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=None,
    ):
        mode, _sub_mode, selected = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "web",
            agent_manager=manager,
        )

    assert selected is web_agent
    assert mode == "agent"
    manager.find_agent_for_session_nowait.assert_not_called()
    manager.get_agent.assert_awaited_once_with(
        channel_id="web",
        mode="agent",
        project_dir=None,
        sub_mode=None,
    )


@pytest.mark.asyncio
async def test_cron_interrupt_resume_reuses_session_owner() -> None:
    owned = MagicMock(name="cron_owner")
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=MagicMock())
    manager.wait_for_session_prewarm = AsyncMock()
    manager.get_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_interrupt_resume_nowait = MagicMock(return_value=owned)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="cron-resume",
        channel_id="web",
        session_id="cron_job1_run2",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "perm-1",
        },
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"mode": "agent", "work_mode": "work"},
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=None,
    ):
        _mode, _sub, selected = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "web",
            agent_manager=manager,
        )

    assert selected is owned
    manager.find_agent_for_interrupt_resume_nowait.assert_called_once_with("cron_job1_run2")
    manager.find_agent_for_session_nowait.assert_not_called()
    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_interrupt_resume_falls_back_to_get_agent_cron_channel() -> None:
    """When no live owner, cron_* interrupt resume falls back to get_agent(__cron__)."""
    agent = MagicMock(name="cron_agent")
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    manager.get_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_session_nowait = MagicMock(return_value=None)
    manager.find_agent_for_interrupt_resume_nowait = MagicMock(return_value=None)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    request = AgentRequest(
        request_id="cron-resume-fallback",
        channel_id="web",
        session_id="cron_job1_run3",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "perm-2",
        },
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"mode": "agent", "work_mode": "work"},
    ), patch.object(
        _default,
        "_sync_chat_request_metadata",
        return_value=None,
    ):
        _mode, _sub, selected = await _default._prepare_code_mode_chat_turn(
            _default_ctx(server, request),
            request,
            "web",
            agent_manager=manager,
        )

    assert selected is agent
    manager.get_agent.assert_awaited_once_with(
        channel_id="__cron__",
        mode="agent",
        project_dir=None,
        sub_mode=None,
    )


@pytest.mark.asyncio
async def test_tenant_pool_plain_cron_followup_does_not_cross_tenant_reuse() -> None:
    owned = MagicMock()
    owned.process_message_stream = MagicMock(
        return_value=_async_chunks(
            [
                SimpleNamespace(
                    request_id="r1",
                    channel_id="web",
                    payload={"event_type": "chat.delta", "content": "ok"},
                    is_complete=True,
                )
            ]
        )
    )
    pool = TenantAgentPool.__new__(TenantAgentPool)
    pool.require_officeclaw_agent = MagicMock(return_value=None)
    pool.find_agent_for_session_nowait = MagicMock(return_value=owned)
    pool._cron_resume_session_allowed = AsyncMock(return_value=True)
    manager = MagicMock()
    manager.process_message_stream = MagicMock(
        return_value=_async_chunks(
            [
                SimpleNamespace(
                    request_id="r1",
                    channel_id="web",
                    payload={"event_type": "chat.delta", "content": "web-ok"},
                    is_complete=True,
                )
            ]
        )
    )
    pool._ensure_agent_manager = AsyncMock(return_value=manager)
    pool._refresh_agent_manager_cache = AsyncMock()
    pool.extract_ids = MagicMock(return_value=("a", "s", "w"))
    pool.resolve_control_rpc_tenant = MagicMock(return_value=("a", "s"))
    pool._build_cache_key = MagicMock(return_value="cache")

    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="cron_tenant_1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "1"},
    )
    chunks = [
        chunk async for chunk in TenantAgentPool.process_message_stream(pool, request)
    ]
    assert len(chunks) == 1
    assert chunks[0].payload["content"] == "web-ok"
    pool.find_agent_for_session_nowait.assert_not_called()
    owned.process_message_stream.assert_not_called()
    pool._ensure_agent_manager.assert_awaited()


@pytest.mark.asyncio
async def test_tenant_pool_reuses_owner_for_interrupt_resume_stream() -> None:
    owned = MagicMock()
    owned.process_message_stream = MagicMock(
        return_value=_async_chunks(
            [
                SimpleNamespace(
                    request_id="r1",
                    channel_id="web",
                    payload={"event_type": "chat.delta", "content": "ok"},
                    is_complete=True,
                )
            ]
        )
    )
    pool = TenantAgentPool.__new__(TenantAgentPool)
    pool.require_officeclaw_agent = MagicMock(return_value=None)
    pool.find_agent_for_session_nowait = MagicMock(return_value=owned)
    pool.find_agent_for_interrupt_resume_nowait = MagicMock(return_value=owned)
    pool._cron_resume_session_allowed = AsyncMock(return_value=True)
    pool._ensure_agent_manager = AsyncMock()

    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="cron_tenant_1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "permission_interrupt",
            "answers": [{"option_id": "allow"}],
            "request_id": "perm-1",
        },
    )
    chunks = [
        chunk async for chunk in TenantAgentPool.process_message_stream(pool, request)
    ]
    assert len(chunks) == 1
    pool._ensure_agent_manager.assert_not_awaited()
    owned.process_message_stream.assert_called_once_with(request)


def _agent_holding(session_id: str, *, live: bool, awaiting: bool, last_used: float):
    adapter = MagicMock()
    adapter._is_session_scoped_adapter = False
    adapter._sessions_awaiting_tool_permission = {session_id} if awaiting or live else set()
    adapter._session_adapter_last_used = {session_id: last_used}
    adapter._session_adapters = {}
    adapter._instance = SimpleNamespace(
        _interaction_session=SimpleNamespace(session_id=session_id),
        _loop_session=None,
    )
    adapter._read_session_interrupt_state = MagicMock(
        return_value={"paused": True} if live else None
    )
    agent = MagicMock()
    agent._adapter = adapter
    return agent


def test_interrupt_resume_prefers_live_web_pause_over_stale_cron_owner() -> None:
    """Follow-up approval must hit the agent that still has the paused tool."""
    cron_agent = _agent_holding("cron_job", live=False, awaiting=True, last_used=1.0)
    web_agent = _agent_holding("cron_job", live=True, awaiting=True, last_used=10.0)
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {
        "__cron__": {"cron": cron_agent},
        "web": {"web": web_agent},
    }
    selected = manager.find_agent_for_interrupt_resume_nowait("cron_job")
    assert selected is web_agent


def test_interrupt_resume_keeps_live_cron_pause() -> None:
    """First-run cron approval still returns to __cron__ when that pause is live."""
    cron_agent = _agent_holding("cron_job", live=True, awaiting=True, last_used=1.0)
    web_agent = _agent_holding("cron_job", live=False, awaiting=False, last_used=9.0)
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {
        "__cron__": {"cron": cron_agent},
        "web": {"web": web_agent},
    }
    selected = manager.find_agent_for_interrupt_resume_nowait("cron_job")
    assert selected is cron_agent


async def _async_chunks(items):
    for item in items:
        yield item
