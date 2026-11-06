# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team.create failure must roll back the reservation and the partial writes.

Two cases, both with max_teams_per_user=1:

1. Binding creation fails after ``reserve_team_create``. No control entity and
   no team directory are left, the lease is released, and another session can
   reserve the only slot.
2. Session metadata write fails after the binding, the team directory, and the
   ``creating`` entity exist. Rollback releases the lease, deletes the entity,
   deletes the binding, and removes the directory. The slot can be reserved again.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.request_identity import apply_routing_metadata
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods
from jiuwenswarm.server.context import RequestContext


@pytest.fixture(autouse=True)
def _register_team_control_methods():
    ensure_team_control_req_methods()
from jiuwenswarm.enterprise.team import handlers
from jiuwenswarm.server.runtime.team_binding_store import (
    TeamBindingStore,
    TeamBindingStoreError,
)
from jiuwenswarm.enterprise.team.control_store import (
    OwnerIdentity,
    SqliteTeamControlStore,
    set_team_control_store,
)
from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore
from jiuwenswarm.enterprise.team.quota import (
    STATUS_RESERVED,
    TeamQuotaService,
    set_team_quota_service,
)
from jiuwenswarm.server.transports.ws_sink import WSSink

OWNER = OwnerIdentity(user_id="u-owner", group_id="grp", bot_id="bot")


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _request(session_id: str) -> AgentRequest:
    return AgentRequest(
        request_id="req-create",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.TEAM_CREATE,
        params={
            "session_id": session_id,
            "team_name": "alpha-team",
            "template_id": "tpl-1",
            "mode": "team",
        },
        metadata=apply_routing_metadata(
            {},
            {"user_id": OWNER.user_id, "group_id": OWNER.group_id, "bot_id": OWNER.bot_id},
        ),
    )


async def _call(request: AgentRequest):
    ws = _FakeWS()
    ctx = RequestContext(
        request=request,
        sink=WSSink(ws, asyncio.Lock()),
        connection_id="c",
        services=None,
    )
    await handlers.handle_team_create(ctx)
    assert ws.sent, "handler must reply"
    resp = parse_agent_server_wire_unary(json.loads(ws.sent[-1]))
    return resp.ok, resp.payload or {}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sessions = tmp_path / "sessions"
    (sessions / "sess-fail").mkdir(parents=True)
    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    quota = TeamQuotaService(sqlite_path=tmp_path / "team_quota.db")
    quota.upsert_policy({
        "policy_id": "x1",
        "match_expr": [],
        "max_teams_per_user": 1,
        "max_teammates_per_team": 5,
    })
    binding_store = TeamBindingStore(tmp_path / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / "teams")
    seen = {"used_after_reserve": None}

    set_team_control_store(control)
    set_team_quota_service(quota)

    real_reserve = quota.reserve_team_create

    def _reserve(identity, session_id):
        lease = real_reserve(identity, session_id)
        seen["used_after_reserve"] = quota.snapshot_user(identity)["teams"]["used"]
        return lease

    monkeypatch.setattr(quota, "reserve_team_create", _reserve)
    monkeypatch.setattr(handlers, "get_agent_sessions_dir", lambda: sessions)

    def _global(name: str):
        if name == "get_config":
            return lambda: {}
        if name == "resolve_agent_request_mode":
            return lambda mode="team": (mode, mode, mode)
        raise AttributeError(name)

    monkeypatch.setattr(handlers, "_server_global", _global)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    from jiuwenswarm.agents.harness.team.config_loader import TeamTemplateNotFoundError

    def _missing_template(*_args, **_kwargs):
        raise TeamTemplateNotFoundError("template missing")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_template_snapshot",
        _missing_template,
    )

    yield {
        "control": control,
        "quota": quota,
        "binding_store": binding_store,
        "entity_store": entity_store,
        "seen": seen,
        "sessions": sessions,
    }

    set_team_control_store(None)
    set_team_quota_service(None)
    control.close()


def _assert_slot_free(env) -> None:
    quota = env["quota"]
    assert quota.snapshot_user(OWNER)["teams"]["used"] == 0
    recycled = quota.reserve_team_create(OWNER, "sess-retry")
    assert recycled.status == STATUS_RESERVED
    assert quota.snapshot_user(OWNER)["teams"]["used"] == 1
    quota.release_lease(recycled)
    assert quota.snapshot_user(OWNER)["teams"]["used"] == 0


@pytest.mark.asyncio
async def test_binding_failure_releases_reservation(env, monkeypatch: pytest.MonkeyPatch):
    def _boom(**_kwargs):
        raise TeamBindingStoreError("binding write failed", code="BAD_REQUEST")

    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.team._create_team_binding_from_template",
        _boom,
    )

    ok, payload = await _call(_request("sess-fail"))

    assert not ok
    assert payload["code"] == "BAD_REQUEST"
    assert "binding write failed" in payload["error"]
    assert env["seen"]["used_after_reserve"] == 1
    assert env["control"].get_entity_by_session("sess-fail") is None
    assert env["binding_store"].get("alpha-team") is None
    assert not (env["entity_store"]._team_path("alpha-team")).exists()
    _assert_slot_free(env)


@pytest.mark.asyncio
async def test_metadata_failure_rolls_back_entity_binding_and_quota(
    env, monkeypatch: pytest.MonkeyPatch
):
    binding_store = env["binding_store"]
    entity_store = env["entity_store"]

    async def _create(*, team_name: str, template_id: str, config_base):
        del config_base
        binding = binding_store.create(team_name=team_name, template_id=template_id)
        team_dir = entity_store._team_path(team_name)
        team_dir.mkdir(parents=True)
        (team_dir / "marker.txt").write_text("created", encoding="utf-8")
        return binding

    def _metadata_fails(**_kwargs):
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.team._create_team_binding_from_template",
        _create,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
        _metadata_fails,
    )

    ok, payload = await _call(_request("sess-fail"))

    assert not ok
    assert payload["code"] == "INTERNAL_ERROR"
    assert "metadata write failed" in payload["error"]
    assert env["seen"]["used_after_reserve"] == 1
    assert env["control"].get_entity_by_session("sess-fail") is None
    assert binding_store.get("alpha-team") is None
    assert not entity_store._team_path("alpha-team").exists()
    _assert_slot_free(env)
