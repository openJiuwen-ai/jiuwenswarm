# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JoinExitHandlers.fetch_team_human_members (路线 B 纯查询契约).

team↔session 一致性真伪的唯一仲裁：按 team_name 直查 team.db 取 human_agent 席位。
返回 list[str]|None：查到→席位名列表，查不到→None，由调用方拼"不存在"文案。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.gateway.message_handler.join_exit_handlers import JoinExitHandlers


class _FakeClient:
    def __init__(self, resp: AgentResponse) -> None:
        self._resp = resp

    async def send_request(self, _env) -> AgentResponse:
        return self._resp


def _make_handler(resp: AgentResponse) -> tuple[JoinExitHandlers, _FakeClient]:
    client = _FakeClient(resp)
    host = SimpleNamespace(agent_client=client)
    return JoinExitHandlers(host), client


@pytest.mark.anyio
async def test_ok_with_members_returns_names() -> None:
    """server ok=True + members → 提取 human_agent 席位名，过滤非 human_agent。"""
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": [
            {"member_id": "reviewer-1", "role": "human_agent"},
            {"member_id": "leader-1", "role": "team_leader"},
            {"member_id": "pm-1", "role": "human_agent"},
        ]},
    )
    h, _ = _make_handler(resp)

    names = await h.fetch_team_human_members("feishu", "sess-1", "team-1")

    assert names == ["reviewer-1", "pm-1"]


@pytest.mark.anyio
async def test_not_ok_returns_none() -> None:
    """server ok=False（team 不存在 / 异常）→ None，文案由调用方拼。"""
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=False,
        payload={"members": []},
    )
    h, _ = _make_handler(resp)

    assert await h.fetch_team_human_members("feishu", "sess-1", "team-1") is None


@pytest.mark.anyio
async def test_ok_but_no_human_agent_returns_none() -> None:
    """server ok=True 但无 human_agent 成员 → 过滤后空 → None。"""
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": [{"member_id": "leader-1", "role": "team_leader"}]},
    )
    h, _ = _make_handler(resp)

    assert await h.fetch_team_human_members("feishu", "sess-1", "team-1") is None


@pytest.mark.anyio
async def test_ok_with_empty_members_returns_none() -> None:
    """server ok=True + 空 members → None。"""
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": []},
    )
    h, _ = _make_handler(resp)

    assert await h.fetch_team_human_members("feishu", "sess-1", "team-1") is None


@pytest.mark.anyio
async def test_rpc_exception_returns_none() -> None:
    """RPC 抛异常 → None，不外泄。"""
    resp = AgentResponse(request_id="r", channel_id="feishu", ok=True, payload={})
    h, client = _make_handler(resp)
    client.send_request = mock.AsyncMock(side_effect=RuntimeError("rpc down"))

    assert await h.fetch_team_human_members("feishu", "sess-1", "team-1") is None
