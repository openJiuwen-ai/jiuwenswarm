# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team.list HTTP catalog branch: params.session_id only, not envelope affinity."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers import organization


class _FakeSink:
    def __init__(self) -> None:
        self.responses: list = []

    async def send_response(self, response) -> None:
        self.responses.append(response)


@pytest.mark.asyncio
async def test_team_list_catalog_ignores_request_affinity_session(monkeypatch):
    """目录态：params 无 session_id，即使 request.session_id 是亲和号也走控制面。"""
    catalog = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.handlers.handle_team_list",
        catalog,
    )
    session_entries = AsyncMock(return_value=[{"team_id": "should-not-run"}])
    monkeypatch.setattr(organization, "_team_list_entries", session_entries)

    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id="shared_user_user1",
        req_method=ReqMethod.TEAM_LIST,
        params={"user_id": "user1", "group_id": "g", "bot_id": "b"},
    )
    sink = _FakeSink()
    ctx = RequestContext(
        request=request,
        sink=sink,
        connection_id="http",
        services=SimpleNamespace(),
    )
    await organization.handle_team_list(ctx)

    catalog.assert_awaited_once_with(ctx)
    session_entries.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_list_session_branch_uses_params_session_id(monkeypatch):
    session_entries = AsyncMock(
        return_value=[{"team_id": "t1", "is_owner": True, "state": "running"}]
    )
    monkeypatch.setattr(organization, "_team_list_entries", session_entries)
    catalog = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.handlers.handle_team_list",
        catalog,
    )

    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id="shared_user_user1",
        req_method=ReqMethod.TEAM_LIST,
        params={"session_id": "web_abc"},
    )
    sink = _FakeSink()
    ctx = RequestContext(
        request=request,
        sink=sink,
        connection_id="http",
        services=SimpleNamespace(),
    )
    await organization.handle_team_list(ctx)

    catalog.assert_not_awaited()
    session_entries.assert_awaited_once_with("web_abc", "web")
    assert sink.responses
    assert sink.responses[-1].ok is True
    assert sink.responses[-1].payload["session_id"] == "web_abc"
    assert sink.responses[-1].payload["default_team_id"] == "t1"
