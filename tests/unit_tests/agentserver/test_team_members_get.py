# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for AgentWebSocketServer._handle_team_members_get (/join seat validation).

路线 B 后 server 退化为纯查询透传：mismatch 校验与对外文案均在 gateway，
server 只查 member、过滤 human_agent、回 ok/members。查不到 → ok=False
（payload 不带文案，gateway 拼"team 不存在"）。
"""

from __future__ import annotations

import asyncio

from jiuwenswarm.server.handlers import team as team_handlers
import json
from typing import Any
from unittest import mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def _ctx_for_test(ws, request, send_lock, server=None):
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    return RequestContext(
        request=request,
        sink=WSSink(ws, send_lock),
        connection_id=str(id(ws)),
        services=AgentServerServices(server) if server is not None else None,
    )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _make_request(
    session_id: str = "sess-1",
    channel_id: str = "feishu",
    team_name: str = "jiwen-team_sess-1",
) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        session_id=session_id,
        channel_id=channel_id,
        req_method=ReqMethod.TEAM_MEMBERS_GET,
        params={"session_id": session_id, "team_name": team_name},
    )


async def _invoke(
    helpers_result: list[dict[str, Any]],
    team_name: str = "jiwen-team_sess-1",
    channel_id: str = "feishu",
    runtime_team_name: str | None = None,
):
    """Call _handle_team_members_get with ``query_team_human_members_for_join`` mocked.

    helpers_result 是 query_team_human_members_for_join 的新返回值（list[dict]，
    未 role 过滤，由 server 过滤 human_agent）。
    """
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.server import agent_ws_server

    ws = _FakeWS()
    lock = asyncio.Lock()
    request = _make_request(team_name=team_name, channel_id=channel_id)

    helper_calls: list[tuple[str, str]] = []

    async def _stub(_session_id, _team_name):
        helper_calls.append((_session_id, _team_name))
        return helpers_result

    with (
        mock.patch(
            "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
            ".query_team_human_members_for_join",
            _stub,
        ),
        mock.patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={
                "team_name": team_name,
                "runtime_team_name": runtime_team_name,
            },
        ),
    ):
        await team_handlers.handle_team_members_get(_ctx_for_test(ws, request, lock))
    assert len(ws.sent) == 1
    return parse_agent_server_wire_unary(json.loads(ws.sent[0])), helper_calls


@pytest.mark.anyio
async def test_returns_human_agent_members() -> None:
    """helpers 返回全部成员 → server 过滤 role==human_agent 后回传。"""
    members = [
        {"member_id": "reviewer-1", "role": "human_agent"},
        {"member_id": "leader-1", "role": "team_leader"},
        {"member_id": "pm-1", "role": "human_agent"},
    ]
    resp, helper_calls = await _invoke(
        members,
        team_name="logical-team",
        runtime_team_name="logical-team_sess-1",
    )

    assert resp.request_id == "req-1"
    assert helper_calls == [("sess-1", "logical-team_sess-1")]
    assert resp.ok is True
    assert [m["member_id"] for m in resp.payload["members"]] == ["reviewer-1", "pm-1"]
    # 路线 B 不再回传 team_name（gateway 不消费）
    assert "team_name" not in resp.payload


@pytest.mark.anyio
async def test_empty_members_returns_not_ok() -> None:
    """helpers 返回空 list（team 不存在 / DB miss）→ server ok=False，不带文案。"""
    resp, _helper_calls = await _invoke([])

    assert resp.ok is False
    assert resp.payload.get("members") == []


@pytest.mark.anyio
async def test_only_non_human_members_returns_not_ok() -> None:
    """helpers 有成员但全是 team_leader（无 human_agent）→ 过滤后空 → ok=False。"""
    resp, _helper_calls = await _invoke([
        {"member_id": "leader-1", "role": "team_leader"},
    ])

    assert resp.ok is False
    assert resp.payload.get("members") == []
