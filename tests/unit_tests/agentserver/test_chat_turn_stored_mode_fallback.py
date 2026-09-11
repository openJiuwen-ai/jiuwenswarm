# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat turn mode 解析：无显式 mode 时以会话锁定的 stored mode 为基准。

跨端续聊（如 xiaoyi 手机控 PC 复用 desktop_* 会话）时 Gateway 不注入渠道
默认 mode，AgentServer 应从会话 metadata 继承已锁定的 mode（team /
design.team / code.*），而不是回落成单 agent。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def _chat_request(session_id: str, *, params: dict) -> AgentRequest:
    return AgentRequest(
        request_id="req_stored_mode",
        channel_id="xiaoyi",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )


def _make_server() -> tuple[AgentWebSocketServer, MagicMock]:
    agent = MagicMock()
    manager = MagicMock()
    manager.get_agent = AsyncMock(return_value=agent)
    manager.wait_for_session_prewarm = AsyncMock()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager
    return server, manager


async def _run_turn(server: AgentWebSocketServer, request: AgentRequest, stored: dict):
    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value=stored,
    ), patch.object(
        agent_ws_server_module,
        "_sync_chat_request_metadata",
        return_value=None,
    ):
        return await server._prepare_code_mode_chat_turn(request, "xiaoyi")


@pytest.mark.asyncio
async def test_no_explicit_mode_inherits_stored_team_mode() -> None:
    """手机续聊 PC 的 team 会话：无显式 mode → 继承锁定的 team。"""
    server, manager = _make_server()
    request = _chat_request("sess_team", params={"query": "继续"})

    mode, sub_mode, _ = await _run_turn(
        server, request, {"mode": "team", "work_mode": "work"}
    )

    assert mode == "team"
    assert sub_mode is None
    assert request.params["mode"] == "team"
    manager.get_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_explicit_mode_inherits_stored_design_team_mode() -> None:
    """design 工作台 team 会话：继承 design.team，不回落成 agent。"""
    server, _ = _make_server()
    request = _chat_request("sess_design_team", params={"query": "继续"})

    mode, sub_mode, _ = await _run_turn(
        server, request, {"mode": "design.team", "work_mode": "design"}
    )

    assert mode == "design"
    assert sub_mode == "team"
    assert request.params["mode"] == "design.team"


@pytest.mark.asyncio
async def test_explicit_mode_overrides_stored_mode() -> None:
    """显式携带 mode（用户 /mode agent 或渠道显式注入）→ stored team 不接管。"""
    server, _ = _make_server()
    request = _chat_request(
        "sess_team_explicit", params={"query": "继续", "mode": "agent"}
    )

    mode, sub_mode, _ = await _run_turn(
        server, request, {"mode": "team", "work_mode": "work"}
    )

    assert mode == "agent"
    assert sub_mode is None
    assert request.params["mode"] == "agent"


@pytest.mark.asyncio
async def test_no_stored_mode_still_defaults_to_agent() -> None:
    """无显式 mode 且会话无锁定 mode（新会话）→ 仍为 agent（行为不变）。"""
    server, _ = _make_server()
    request = _chat_request("sess_fresh", params={"query": "你好"})

    mode, sub_mode, _ = await _run_turn(server, request, {})

    assert mode == "agent"
    assert sub_mode is None
    assert request.params["mode"] == "agent"
