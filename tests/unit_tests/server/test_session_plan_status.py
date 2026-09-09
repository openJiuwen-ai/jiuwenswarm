# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session.plan_status：刷新后恢复「计划」标签的只读查询。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def _request(session_id: str | None, *, params: dict | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="req-plan-status",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.SESSION_PLAN_STATUS,
        params=params if params is not None else ({"session_id": session_id} if session_id else {}),
    )


def _server(*, agents: dict | None = None) -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = SimpleNamespace(agents=agents or {})
    return server


async def _call_handler(server: AgentWebSocketServer, request: AgentRequest) -> AgentResponse:
    captured: dict[str, AgentResponse] = {}

    def _encode(resp, response_id=None):  # noqa: ARG001
        captured["resp"] = resp
        return {"encoded": True}

    send_lock = AsyncMock()
    send_lock.__aenter__ = AsyncMock(return_value=None)
    send_lock.__aexit__ = AsyncMock(return_value=None)
    with (
        patch(
            "jiuwenswarm.server.agent_ws_server.encode_agent_response_for_wire",
            side_effect=_encode,
        ),
        patch(
            "jiuwenswarm.server.agent_ws_server.send_wire_payload",
            new=AsyncMock(),
        ),
    ):
        await server._handle_session_plan_status(object(), request, send_lock)
    return captured["resp"]


def test_combine_live_plan_mode_wins_over_metadata():
    """live plan_mode=normal 时，即使 metadata 仍是 *.plan 也算已退出。"""
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode="normal",
            session_id="s1",
            metadata_mode="agent.work.plan",
        )
        is False
    )
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode="plan",
            session_id="s1",
            metadata_mode="agent.work.normal",
        )
        is True
    )


def test_combine_team_metadata_ignores_live_plan_mode():
    """集群 plan 以 metadata.mode 为准，不被同 session 上 DeepAgent 的默认 normal 盖掉。"""
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode="normal",
            session_id="s-team",
            metadata_mode="team.work.plan",
        )
        is True
    )
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode="plan",
            session_id="s-team",
            metadata_mode="team.work.normal",
        )
        is False
    )
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode="normal",
            session_id="s-team",
            metadata_mode="team.code.plan",
        )
        is True
    )


def test_combine_falls_back_to_metadata_and_process_marker():
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode=None,
            session_id="s1",
            metadata_mode="team.work.plan",
        )
        is True
    )
    assert (
        AgentWebSocketServer._combine_session_in_plan(
            live_plan_mode=None,
            session_id="s1",
            metadata_mode="agent.work.normal",
        )
        is False
    )
    agent_ws_server_module._plan_active_sessions.add("s-marked")
    try:
        assert (
            AgentWebSocketServer._combine_session_in_plan(
                live_plan_mode=None,
                session_id="s-marked",
                metadata_mode="agent",
            )
            is True
        )
    finally:
        agent_ws_server_module._plan_active_sessions.discard("s-marked")


def test_try_read_live_plan_mode_from_cached_agent():
    session_id = "s-live"
    deep_agent = SimpleNamespace(
        load_state=lambda _session: SimpleNamespace(plan_mode=SimpleNamespace(mode="plan")),
    )
    agent = SimpleNamespace(get_live_session_instance=lambda sid: deep_agent if sid == session_id else None)
    server = _server(agents={"web": {"agent::": agent}})
    with patch(
        "jiuwenswarm.agents.harness.common.session_ops_service.resolve_live_agent_session",
        return_value=object(),
    ):
        assert server._try_read_live_plan_mode(session_id) == "plan"
        assert server._try_read_live_plan_mode("other") is None


@pytest.mark.asyncio
async def test_handler_requires_session_id():
    resp = await _call_handler(_server(), _request(None, params={}))
    assert resp.ok is False
    assert resp.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_handler_missing_session_is_not_found():
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={},
    ) as get_meta:
        resp = await _call_handler(_server(), _request("missing"))
    assert resp.ok is False
    assert resp.payload["code"] == "NOT_FOUND"
    get_meta.assert_called_once_with("missing", cache_bust=True, enable_writeback=False)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("agent.work.plan", True),
        ("agent.code.plan", True),
        ("team.work.plan", True),
        ("team.code.plan", True),
        ("agent.work.normal", False),
        ("team.work.normal", False),
    ],
)
@pytest.mark.asyncio
async def test_handler_uses_metadata_when_no_live_agent(mode, expected):
    original_meta = {"session_id": "s1", "mode": mode}
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=dict(original_meta),
    ):
        resp = await _call_handler(_server(), _request("s1"))
    assert resp.ok is True
    assert resp.payload == {"session_id": "s1", "in_plan": expected}
    assert original_meta["mode"] == mode


@pytest.mark.asyncio
async def test_handler_live_plan_mode_overrides_stale_metadata():
    server = _server()
    server._try_read_live_plan_mode = MagicMock(return_value="normal")
    meta = {"session_id": "s1", "mode": "agent.work.plan"}
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=meta,
    ):
        resp = await _call_handler(server, _request("s1"))
    assert resp.ok is True
    assert resp.payload["in_plan"] is False
    assert meta["mode"] == "agent.work.plan"
    assert "s1" not in agent_ws_server_module._plan_active_sessions


@pytest.mark.asyncio
async def test_handler_team_plan_ignores_live_agent_plan_mode():
    server = _server()
    server._try_read_live_plan_mode = MagicMock(return_value="normal")
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"session_id": "s-team", "mode": "team.work.plan"},
    ):
        resp = await _call_handler(server, _request("s-team"))
    assert resp.ok is True
    assert resp.payload == {"session_id": "s-team", "in_plan": True}
    server._try_read_live_plan_mode.assert_not_called()


@pytest.mark.asyncio
async def test_handler_does_not_start_or_switch_plan_mode():
    agent = MagicMock()
    agent.get_live_session_instance.return_value = None
    agent.switch_mode = MagicMock()
    agent.ensure_live_session_instance = MagicMock()
    server = _server(agents={"web": {"k": agent}})
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"mode": "agent.work.plan"},
    ):
        await _call_handler(server, _request("s1"))
    agent.switch_mode.assert_not_called()
    agent.ensure_live_session_instance.assert_not_called()


def test_web_handler_registered():
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        WebHandlersBindParams,
        _register_web_handlers,
    )

    class _FakeChannel:
        def __init__(self):
            self.methods: dict[str, object] = {}

        def register_method(self, name, handler):
            self.methods[name] = handler

        def on_connect(self, handler):
            pass

    channel = _FakeChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    assert "session.plan_status" in channel.methods
    assert ReqMethod.SESSION_PLAN_STATUS.value == "session.plan_status"
