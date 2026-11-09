# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise team.create surfaces X/Z quota as a user-facing failure."""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.common.request_identity import apply_routing_metadata
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.enterprise.team import handlers as team_control
from jiuwenswarm.enterprise.team.control_store import SqliteTeamControlStore
from jiuwenswarm.enterprise.team.quota import (
    KIND_ORG_TEAM_CREATE,
    KIND_TEAM_CREATE,
    QUOTA_EXCEEDED,
    TeamQuotaExceeded,
    org_team_create_quota_user_message,
    team_create_quota_user_message,
    team_quota_user_message,
)
from jiuwenswarm.server.transports.ws_sink import WSSink


@pytest.fixture(autouse=True)
def _register_team_control_methods():
    ensure_team_control_req_methods()


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _request(session_id: str) -> AgentRequest:
    return AgentRequest(
        request_id="req-quota",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.TEAM_CREATE,
        params={"session_id": session_id, "description": "做一个三人团队"},
        metadata=apply_routing_metadata(
            {},
            {"user_id": "u-1", "group_id": "grp", "bot_id": "bot"},
        ),
    )


async def _create(monkeypatch, tmp_path, exc: TeamQuotaExceeded) -> dict:
    session_id = "sess-quota"
    root = tmp_path / "sessions"
    (root / session_id).mkdir(parents=True)
    store = SqliteTeamControlStore(tmp_path / "team_control.db")
    monkeypatch.setattr(team_control, "get_team_control_store", lambda: store)
    monkeypatch.setattr(team_control, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(
        team_control,
        "_server_global",
        lambda name: (lambda *args, **kwargs: ("team", "team", "team"))
        if name == "resolve_agent_request_mode"
        else (lambda: {}),
    )

    class _Quota:
        def resolve_limits(self, _identity):
            return type("Limits", (), {"max_teammates_per_team": 5, "default_teammates": 5})()

        def reserve_team_create(self, _identity, _session_id):
            raise exc

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_team_quota_service",
        lambda: _Quota(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.count_predefined_non_leaders",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.teammate_spawn_slots",
        lambda limit: max(int(limit) - 1, 0),
    )
    ws = _FakeWS()
    ctx = RequestContext(
        request=_request(session_id),
        sink=WSSink(ws, asyncio.Lock()),
        connection_id="c",
        services=None,  # type: ignore[arg-type]
    )
    await team_control.handle_team_create(ctx)
    wire = json.loads(ws.sent[-1])
    body = wire.get("body") if isinstance(wire.get("body"), dict) else wire
    return body if isinstance(body, dict) else {}


@pytest.mark.asyncio
async def test_team_create_reports_user_quota(monkeypatch, tmp_path):
    exc = TeamQuotaExceeded(
        "team quota exceeded: 5/5",
        kind=KIND_TEAM_CREATE,
        used=5,
        limit=5,
        default=5,
    )
    payload = await _create(monkeypatch, tmp_path, exc)
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    code = details.get("code") or nested.get("code") or payload.get("code")
    assert code == QUOTA_EXCEEDED
    text = json.dumps(payload, ensure_ascii=False)
    assert team_create_quota_user_message(5) in text
    assert team_quota_user_message(exc) in text


@pytest.mark.asyncio
async def test_team_create_reports_org_quota(monkeypatch, tmp_path):
    exc = TeamQuotaExceeded(
        "team quota exceeded: 2/2",
        kind=KIND_ORG_TEAM_CREATE,
        used=2,
        limit=2,
        default=0,
    )
    payload = await _create(monkeypatch, tmp_path, exc)
    text = json.dumps(payload, ensure_ascii=False)
    assert org_team_create_quota_user_message(2) in text
