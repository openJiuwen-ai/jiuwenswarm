# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer enterprise team control handlers (team.get / list / stop / members)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway_protocol.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.request_identity import apply_routing_metadata
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.enterprise.team import handlers
from jiuwenswarm.enterprise.team.control_store import (
    MEMBERSHIP_STATUS_ACTIVE,
    TEAM_STATUS_STOPPED,
    OwnerIdentity,
    SqliteTeamControlStore,
    set_team_control_store,
)
from jiuwenswarm.server.transports.ws_sink import WSSink

@pytest.fixture(autouse=True)
def _register_team_control_methods():
    ensure_team_control_req_methods()


OWNER = OwnerIdentity(user_id="u-owner", group_id="grp", bot_id="bot")
MEMBER = OwnerIdentity(user_id="u-member", group_id="grp", bot_id="bot")


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _request(method: ReqMethod, identity: OwnerIdentity, params: dict, session_id: str | None = None):
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id=session_id,
        req_method=method,
        params=dict(params),
        metadata=apply_routing_metadata(
            {},
            {"user_id": identity.user_id, "group_id": identity.group_id, "bot_id": identity.bot_id},
        ),
    )


async def _call(handler, request):
    ws = _FakeWS()
    ctx = RequestContext(request=request, sink=WSSink(ws, asyncio.Lock()), connection_id="c", services=None)
    await handler(ctx)
    assert ws.sent, "handler must reply"
    resp = parse_agent_server_wire_unary(json.loads(ws.sent[-1]))
    return resp.ok, resp.payload or {}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SqliteTeamControlStore(tmp_path / "team_control.db")
    set_team_control_store(s)
    # no live runtimes in unit tests
    monkeypatch.setattr(handlers, "_runtime_state", lambda entity: {"runtime_active": False, "runtime_pending": False})
    yield s
    set_team_control_store(None)
    s.close()


@pytest.fixture
def entity(store):
    return store.create_entity(
        team_name="alpha", owner=OWNER, template_id="jiuwen_team",
        team_session_id="sess-1", runtime_team_name="alpha_sess1",
    )


def test_request_identity_reads_routing_triple():
    req = _request(ReqMethod.TEAM_GET, OWNER, {})
    assert handlers.request_identity(req) == OWNER


@pytest.mark.asyncio
async def test_team_get_by_session_for_owner(store, entity):
    ok, payload = await _call(handlers.handle_team_get, _request(ReqMethod.TEAM_GET, OWNER, {}, session_id="sess-1"))
    assert ok
    assert payload["team"]["team_id"] == entity.team_id
    assert payload["team"]["owner_user_id"] == "u-owner"
    assert payload["team"]["memberships"] == []


@pytest.mark.asyncio
async def test_team_get_forbidden_for_non_member(store, entity):
    ok, payload = await _call(handlers.handle_team_get, _request(ReqMethod.TEAM_GET, MEMBER, {"team_id": entity.team_id}))
    assert not ok and payload["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_team_get_not_found(store):
    ok, payload = await _call(handlers.handle_team_get, _request(ReqMethod.TEAM_GET, OWNER, {"team_id": "nope"}))
    assert not ok and payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_team_list_includes_owned_and_joined(store, entity):
    other = store.create_entity(team_name="beta", owner=MEMBER, team_session_id="sess-2")
    store.upsert_membership(team_id=other.team_id, identity=OWNER, member_name="human-o",
                            status=MEMBERSHIP_STATUS_ACTIVE)
    store.create_entity(team_name="gamma", owner=OwnerIdentity("u-z", "grp", "bot"))

    ok, payload = await _call(handlers.handle_team_list, _request(ReqMethod.TEAM_LIST, OWNER, {}))
    assert ok
    assert {t["team_name"] for t in payload["teams"]} == {"alpha", "beta"}
    assert payload["count"] == 2


@pytest.mark.asyncio
async def test_team_stop_owner_only(store, entity, monkeypatch):
    calls = []

    async def _stop(*, session_id, reason):
        calls.append(session_id)
        return True

    import jiuwenswarm.agents.harness.team as harness_team

    monkeypatch.setattr(harness_team, "stop_team_session_runtime_across_managers", _stop)

    ok, payload = await _call(handlers.handle_team_stop, _request(ReqMethod.TEAM_STOP, MEMBER, {"team_id": entity.team_id}))
    assert not ok and payload["code"] == "FORBIDDEN"

    ok, payload = await _call(handlers.handle_team_stop, _request(ReqMethod.TEAM_STOP, OWNER, {"team_id": entity.team_id}))
    assert ok
    assert payload["runtime_stopped"] is True
    assert payload["team"]["status"] == TEAM_STATUS_STOPPED
    assert calls == ["sess-1"]


@pytest.mark.asyncio
async def test_members_list_shows_my_membership(store, entity):
    store.upsert_membership(team_id=entity.team_id, identity=MEMBER, member_name="human-m",
                            status=MEMBERSHIP_STATUS_ACTIVE)
    ok, payload = await _call(
        handlers.handle_team_members_list,
        _request(ReqMethod.TEAM_MEMBERS_LIST, MEMBER, {"session_id": "sess-1"}),
    )
    assert ok
    assert payload["my_membership"]["member_name"] == "human-m"
    assert payload["is_owner"] is False
    assert [m["member_name"] for m in payload["memberships"]] == ["human-m"]


@pytest.mark.asyncio
async def test_member_join_requires_identity(store, entity):
    req = AgentRequest(request_id="r", channel_id="web", session_id="sess-1",
                       req_method=ReqMethod.TEAM_MEMBER_JOIN, params={})
    ok, payload = await _call(handlers.handle_team_member_join, req)
    assert not ok and payload["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_member_join_and_leave_via_handlers(store, entity, monkeypatch):
    from jiuwenswarm.enterprise.team import membership as svc

    async def _no_backend(_name):
        return None

    async def _no_roles(_name):
        return {}

    monkeypatch.setattr(svc, "_active_team_backend", _no_backend)
    monkeypatch.setattr(svc, "_db_member_roles", _no_roles)
    monkeypatch.setattr(svc, "_broadcast_membership", lambda *a, **k: None)

    ok, payload = await _call(
        handlers.handle_team_member_join,
        _request(ReqMethod.TEAM_MEMBER_JOIN, MEMBER, {"session_id": "sess-1", "member_name": "human-bob"}),
    )
    assert ok
    assert payload["membership"]["status"] == "pending"
    assert payload["seat_ready"] is False
    assert payload["session_id"] == "sess-1"

    ok, payload = await _call(
        handlers.handle_team_member_leave,
        _request(ReqMethod.TEAM_MEMBER_LEAVE, MEMBER, {"team_id": entity.team_id}),
    )
    assert ok and payload["membership"]["status"] == "left"
