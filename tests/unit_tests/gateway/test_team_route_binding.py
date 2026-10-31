# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway enterprise Team: route binding store + Web membership registry."""

from __future__ import annotations

import pytest

from jiuwenswarm.enterprise.team.membership_registry import TeamMembershipRegistry
from jiuwenswarm.enterprise.team.route_binding_store import (
    ROUTE_STATUS_ACTIVE,
    ROUTE_STATUS_UNHEALTHY,
    InMemoryTeamRouteBindingStore,
    SqliteTeamRouteBindingStore,
)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        s = InMemoryTeamRouteBindingStore()
    else:
        s = SqliteTeamRouteBindingStore(tmp_path / "routes.db")
    yield s
    s.close()


def test_upsert_insert_then_update_bumps_version_on_endpoint_change(store):
    b1 = store.upsert(
        team_session_id="sess-1", endpoint="http://10.0.0.1:8086",
        agent_service_id="pod-1", route_request_id="r1", owner=("g1", "b1", "u1"),
    )
    assert b1.route_version == 1 and b1.has_owner
    assert b1.owner_triple() == ("g1", "b1", "u1")

    same = store.upsert(team_session_id="sess-1", endpoint="http://10.0.0.1:8086",
                        agent_service_id="pod-1", route_request_id="r2")
    assert same.route_version == 1
    assert same.route_request_id == "r2"

    moved = store.upsert(team_session_id="sess-1", endpoint="http://10.0.0.2:8086",
                         agent_service_id="pod-2", route_request_id="r3")
    assert moved.route_version == 2
    assert moved.agent_service_id == "pod-2"
    # owner preserved across updates
    assert moved.owner_triple() == ("g1", "b1", "u1")


def test_owner_is_write_once(store):
    store.upsert(team_session_id="s", endpoint="http://a", owner=("g1", "b1", "owner"))
    b = store.upsert(team_session_id="s", endpoint="http://a", owner=("g1", "b1", "member"))
    assert b.owner_user_id == "owner"
    # a session bound without owner picks the first owner offered later
    store.upsert(team_session_id="s2", endpoint="http://a")
    b2 = store.upsert(team_session_id="s2", endpoint="http://a", owner=("g1", "b1", "late"))
    assert b2.owner_user_id == "late"


def test_team_id_backfill_and_lookup(store):
    store.upsert(team_session_id="sess-1", endpoint="http://a")
    assert store.get_by_team_id("T1") is None
    b = store.upsert(team_session_id="sess-1", endpoint="http://a", team_id="T1")
    assert b.team_id == "T1"
    assert store.get_by_team_id("T1").team_session_id == "sess-1"
    # empty team_id does not erase
    b2 = store.upsert(team_session_id="sess-1", endpoint="http://a", team_id="")
    assert b2.team_id == "T1"


def test_status_touch_delete_list(store):
    store.upsert(team_session_id="s1", endpoint="http://a")
    store.upsert(team_session_id="s2", endpoint="http://b")
    assert {b.team_session_id for b in store.list_all()} == {"s1", "s2"}

    marked = store.mark_status("s1", ROUTE_STATUS_UNHEALTHY)
    assert marked.status == ROUTE_STATUS_UNHEALTHY
    assert store.mark_status("missing", ROUTE_STATUS_ACTIVE) is None

    before = store.get_by_session("s2").last_heartbeat_at
    store.touch("s2")
    assert store.get_by_session("s2").last_heartbeat_at >= before

    assert store.delete("s1") is True
    assert store.delete("s1") is False
    assert store.get_by_session("s1") is None


def test_upsert_requires_session_and_endpoint(store):
    with pytest.raises(ValueError):
        store.upsert(team_session_id="", endpoint="http://a")
    with pytest.raises(ValueError):
        store.upsert(team_session_id="s", endpoint="")


# ---------------------------------------------------------------- registry


def test_membership_registry_roundtrip():
    reg = TeamMembershipRegistry()
    assert reg.register(channel_id="web", user_id="u1", session_id="s1",
                        team_id="T1", member_name="human-a") is not None
    assert reg.member_name_for("WEB", "u1", "s1") == "human-a"
    assert reg.member_name_for("web", "u1", "other") == ""
    assert reg.member_name_for("feishu", "u1", "s1") == ""
    assert [m.session_id for m in reg.sessions_for_user("web", "u1")] == ["s1"]

    assert reg.unregister(channel_id="web", user_id="u1", session_id="s1")
    assert not reg.unregister(channel_id="web", user_id="u1", session_id="s1")
    assert reg.member_name_for("web", "u1", "s1") == ""


def test_membership_registry_rejects_incomplete_keys():
    reg = TeamMembershipRegistry()
    assert reg.register(channel_id="web", user_id="", session_id="s1", team_id="T", member_name="x") is None
    assert reg.register(channel_id="web", user_id="u", session_id="s1", team_id="T", member_name="") is None
    assert reg.lookup("web", "", "s1") is None
