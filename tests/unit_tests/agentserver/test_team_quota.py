# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SQLite TeamQuotaService: reserve / commit / release and policy selection."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jiuwenswarm.enterprise.team.control_store import (
    OwnerIdentity,
    SqliteTeamControlStore,
    set_team_control_store,
)
from jiuwenswarm.enterprise.team.quota import (
    DEFAULT_MAX_TEAMS_PER_USER,
    DEFAULT_MAX_TEAMMATES_PER_TEAM,
    KIND_ORG_TEAM_CREATE,
    KIND_TEAM_CREATE,
    KIND_TEAMMATE_SPAWN,
    STATUS_COMMITTED,
    STATUS_RESERVED,
    TeamQuotaExceeded,
    TeamQuotaService,
    count_predefined_non_leaders,
    org_team_create_quota_user_message,
    set_team_quota_service,
    team_create_quota_user_message,
    team_quota_user_message,
    teammate_spawn_key,
    teammate_spawn_slots,
)


def _ident(user: str = "u1", group: str = "g1", bot: str = "b1") -> OwnerIdentity:
    return OwnerIdentity(user_id=user, group_id=group, bot_id=bot)


def _svc(tmp_path: Path) -> TeamQuotaService:
    return TeamQuotaService(sqlite_path=tmp_path / "team_quota.db")


def test_default_limits_without_policy(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    limits = svc.resolve_limits(_ident())
    assert limits.max_teams_per_user == DEFAULT_MAX_TEAMS_PER_USER
    assert limits.max_teammates_per_team == DEFAULT_MAX_TEAMMATES_PER_TEAM


def test_highest_priority_match_wins(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "wide",
        "priority": 1,
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
    })
    svc.upsert_policy({
        "policy_id": "user",
        "priority": 10,
        "match_expr": "user_id == 'u1'",
        "max_teams_per_user": 2,
        "max_teammates_per_team": 3,
    })
    hit = svc.resolve_limits(_ident("u1"))
    miss = svc.resolve_limits(_ident("u2"))
    assert hit.max_teams_per_user == 2
    assert hit.max_teammates_per_team == 3
    assert hit.policy_id == "user"
    assert miss.max_teams_per_user == 5
    assert miss.default_teams == 5


def test_same_priority_later_save_wins(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    body = {
        "priority": 5,
        "match_expr": "user_id == 'u1'",
        "max_teammates_per_team": 4,
        "max_teams_per_org": 0,
    }
    svc.upsert_policy({**body, "policy_id": "older", "max_teams_per_user": 2})
    svc.upsert_policy({**body, "policy_id": "newer", "max_teams_per_user": 8})
    conn = svc._sqlite()
    conn.execute(
        "UPDATE team_quota_policy SET updated_at=? WHERE policy_id=?",
        ("2026-01-01T00:00:01Z", "older"),
    )
    conn.execute(
        "UPDATE team_quota_policy SET updated_at=? WHERE policy_id=?",
        ("2026-01-01T00:00:02Z", "newer"),
    )
    hit = svc.resolve_limits(_ident("u1"))
    assert hit.policy_id == "newer"
    assert hit.max_teams_per_user == 8

    svc.upsert_policy({**body, "policy_id": "older", "max_teams_per_user": 2})
    edited = svc.resolve_limits(_ident("u1"))
    assert edited.policy_id == "older"
    assert edited.max_teams_per_user == 2


def test_y_follows_owner_not_caller(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "owner",
        "priority": 20,
        "match_expr": "user_id == 'owner'",
        "max_teams_per_user": 5,
        "max_teammates_per_team": 2,
    })
    owner = svc.resolve_limits(_ident("owner"))
    member = svc.resolve_limits(_ident("member"))
    assert owner.max_teammates_per_team == 2
    assert member.max_teammates_per_team == DEFAULT_MAX_TEAMMATES_PER_TEAM


def test_reserve_commit_release_and_idempotent_key(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    ident = _ident()
    first = svc.reserve_team_create(ident, "sess-a")
    assert first.status == STATUS_RESERVED
    again = svc.reserve_team_create(ident, "sess-a")
    assert again.lease_id == first.lease_id
    svc.commit_lease(first, team_id="tid-1")
    snap = svc.snapshot_user(ident)
    assert snap["teams"]["used"] == 1
    assert snap["teams"]["occupying_ids"] == ["tid-1"]
    svc.release_lease(first)
    svc.release_lease(first)
    assert svc.snapshot_user(ident)["teams"]["used"] == 0
    recycled = svc.reserve_team_create(ident, "sess-a")
    assert recycled.status == STATUS_RESERVED
    assert recycled.lease_id != first.lease_id


def test_empty_user_concurrent_create_does_not_oversell(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "x1",
        "match_expr": [],
        "max_teams_per_user": 1,
        "max_teammates_per_team": 5,
    })
    ident = _ident()

    def _one(i: int):
        try:
            return svc.reserve_team_create(ident, f"sess-{i}")
        except TeamQuotaExceeded as exc:
            return exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one, range(4)))
    ok = [r for r in results if not isinstance(r, TeamQuotaExceeded)]
    denied = [r for r in results if isinstance(r, TeamQuotaExceeded)]
    assert len(ok) == 1
    assert len(denied) == 3
    assert denied[0].kind == KIND_TEAM_CREATE


def test_spawn_y_limit_and_release(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "y2",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 3,
    })
    owner = _ident()
    x = svc.reserve_team_create(owner, "sess-t")
    svc.commit_lease(x, team_id="tid-y")
    assert svc.snapshot_team("tid-y", owner)["used"] == 1
    a = svc.reserve_teammate_spawn(team_id="tid-y", member_name="a", owner=owner)
    b = svc.reserve_teammate_spawn(team_id="tid-y", member_name="b", owner=owner)
    svc.commit_lease(a, team_id="tid-y", member_name="a")
    svc.commit_lease(b, team_id="tid-y", member_name="b")
    with pytest.raises(TeamQuotaExceeded) as exc:
        svc.reserve_teammate_spawn(team_id="tid-y", member_name="c", owner=owner)
    assert exc.value.kind == KIND_TEAMMATE_SPAWN
    assert exc.value.used == 3
    assert exc.value.limit == 3
    svc.release_teammate("tid-y", "a")
    c = svc.reserve_teammate_spawn(team_id="tid-y", member_name="c", owner=owner)
    assert c.status == STATUS_RESERVED
    svc.release_team("tid-y")
    assert svc.snapshot_user(owner)["teams"]["used"] == 0
    assert svc.snapshot_team("tid-y", owner)["used"] == 0


def test_n3_allows_two_spawns_leader_does_not_occupy(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "n3",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 3,
    })
    owner = _ident()
    x = svc.reserve_team_create(owner, "sess-n3")
    svc.commit_lease(x, team_id="tid-n3")
    leader = svc.reserve_teammate_spawn(
        team_id="tid-n3", member_name="team-leader", owner=owner,
    )
    assert leader.noop
    a = svc.reserve_teammate_spawn(team_id="tid-n3", member_name="frontend-dev", owner=owner)
    b = svc.reserve_teammate_spawn(team_id="tid-n3", member_name="backend-dev", owner=owner)
    svc.commit_lease(a, team_id="tid-n3", member_name="frontend-dev")
    svc.commit_lease(b, team_id="tid-n3", member_name="backend-dev")
    with pytest.raises(TeamQuotaExceeded) as exc:
        svc.reserve_teammate_spawn(team_id="tid-n3", member_name="qa-engineer", owner=owner)
    assert exc.value.used == 3
    assert exc.value.limit == 3
    assert svc.snapshot_team("tid-n3", owner)["used"] == 3


def test_stale_leader_spawn_lease_does_not_consume_y_slot(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "n3",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 3,
    })
    owner = _ident()
    x = svc.reserve_team_create(owner, "sess-stale")
    svc.commit_lease(x, team_id="tid-stale")
    now = time.time()
    with svc._lock:
        svc._sqlite().execute(
            "INSERT INTO team_quota_lease ("
            "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
            "team_id, member_name, status, expires_at, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "stale-leader",
                KIND_TEAMMATE_SPAWN,
                "team:tid-stale:member:team-leader",
                owner.user_id,
                owner.group_id,
                owner.bot_id,
                "tid-stale",
                "team-leader",
                STATUS_COMMITTED,
                None,
                now,
                now,
            ),
        )
    a = svc.reserve_teammate_spawn(team_id="tid-stale", member_name="frontend-dev", owner=owner)
    b = svc.reserve_teammate_spawn(team_id="tid-stale", member_name="backend-dev", owner=owner)
    assert a.status == STATUS_RESERVED
    assert b.status == STATUS_RESERVED
    with pytest.raises(TeamQuotaExceeded):
        svc.reserve_teammate_spawn(team_id="tid-stale", member_name="qa-engineer", owner=owner)


def test_failed_create_releases_lease(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    ident = _ident()
    lease = svc.reserve_team_create(ident, "sess-fail")
    svc.release_lease(lease)
    assert svc.snapshot_user(ident)["teams"]["used"] == 0


def test_template_over_y_counted(tmp_path: Path) -> None:
    cfg = {
        "modes": {
            "team": {
                "t1": {
                    "predefined_members": [
                        {"member_name": "leader", "role": "leader"},
                        {"member_name": "a", "role": "teammate"},
                        {"member_name": "b", "role": "teammate"},
                        {"member_name": "c", "role": "teammate"},
                    ]
                }
            }
        }
    }
    assert count_predefined_non_leaders(cfg, "t1") == 3
    assert count_predefined_non_leaders({"team": {}}, None) == 0
    assert teammate_spawn_slots(1) == 0
    assert teammate_spawn_slots(3) == 2
    assert teammate_spawn_slots(5) == 4


def test_reconcile_local_roster_only_touches_given_team(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    owner = _ident()
    x = svc.reserve_team_create(owner, "sess-local")
    svc.commit_lease(x, team_id="local")
    foreign = svc.reserve_teammate_spawn(team_id="other", member_name="ghost", owner=owner)
    svc.commit_lease(foreign, team_id="other", member_name="ghost")
    live = svc.reserve_teammate_spawn(team_id="local", member_name="keep", owner=owner)
    svc.commit_lease(live, team_id="local", member_name="keep")
    stale = svc.reserve_teammate_spawn(team_id="local", member_name="gone", owner=owner)
    svc.commit_lease(stale, team_id="local", member_name="gone")

    svc._owner_of_team = lambda _tid: owner  # type: ignore[method-assign]
    svc.reconcile_local_roster("local", ["keep", "new"])
    local_names = {
        row["member_name"]
        for row in svc._list_occupying(KIND_TEAMMATE_SPAWN, team_id="local")
    }
    other_names = {
        row["member_name"]
        for row in svc._list_occupying(KIND_TEAMMATE_SPAWN, team_id="other")
    }
    assert local_names == {"keep", "new"}
    assert other_names == {"ghost"}
    assert teammate_spawn_key("local", "keep") == "team:local:member:keep"


def test_disabled_flag_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_QUOTA_ENABLED", "0")
    svc = _svc(tmp_path)
    lease = svc.reserve_team_create(_ident(), "sess-off")
    assert lease.noop


def test_team_create_quota_user_message() -> None:
    assert team_create_quota_user_message(5) == "创建失败，一个用户最多有5个团队"


def test_mark_team_deleted_releases_x_quota(tmp_path: Path) -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )
    from jiuwenswarm.enterprise.team.control_store import (
        TEAM_STATUS_RUNNING,
        SqliteTeamControlStore,
        set_team_control_store,
    )
    from jiuwenswarm.enterprise.team.session_cleanup import (
        purge_team_for_deleted_session,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(InMemoryTeamRouteBindingStore())
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="alpha",
            owner=ident,
            team_session_id="sess-del",
            status=TEAM_STATUS_RUNNING,
        )
        control.upsert_membership(
            team_id=entity.team_id,
            identity=ident,
            member_name="owner",
            status="active",
        )
        lease = svc.reserve_team_create(ident, "sess-del")
        svc.commit_lease(lease, team_id=entity.team_id)
        spawn = svc.reserve_teammate_spawn(
            team_id=entity.team_id,
            member_name="qa",
            owner=ident,
        )
        svc.commit_lease(spawn, team_id=entity.team_id, member_name="qa")
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
        assert purge_team_for_deleted_session(session_id="sess-del") == entity.team_id
        assert control.get_entity(entity.team_id) is None
        assert control.list_memberships(entity.team_id) == []
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
        leftover = svc._sqlite().execute("SELECT lease_id FROM team_quota_lease").fetchall()
        assert leftover == []
        assert purge_team_for_deleted_session(session_id="sess-del") is None
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        set_team_route_binding_store(None)


def test_purge_drops_orphan_session_lease_without_entity(tmp_path: Path) -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )
    from jiuwenswarm.enterprise.team.control_store import (
        SqliteTeamControlStore,
        set_team_control_store,
    )
    from jiuwenswarm.enterprise.team.session_cleanup import (
        purge_team_for_deleted_session,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(InMemoryTeamRouteBindingStore())
    try:
        ident = _ident()
        lease = svc.reserve_team_create(ident, "sess-orphan")
        svc.commit_lease(lease)
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
        assert purge_team_for_deleted_session(session_id="sess-orphan") is None
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
        leftover = svc._sqlite().execute("SELECT lease_id FROM team_quota_lease").fetchall()
        assert leftover == []
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        set_team_route_binding_store(None)


def test_purge_drops_y_leases_via_create_lease_team_id(tmp_path: Path) -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )
    from jiuwenswarm.enterprise.team.control_store import (
        TEAM_STATUS_RUNNING,
        SqliteTeamControlStore,
        set_team_control_store,
    )
    from jiuwenswarm.enterprise.team.session_cleanup import (
        purge_team_for_deleted_session,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(InMemoryTeamRouteBindingStore())
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="ghost",
            owner=ident,
            team_session_id="sess-orphan-y",
            status=TEAM_STATUS_RUNNING,
        )
        lease = svc.reserve_team_create(ident, "sess-orphan-y")
        svc.commit_lease(lease, team_id=entity.team_id)
        spawn = svc.reserve_teammate_spawn(
            team_id=entity.team_id,
            member_name="qa",
            owner=ident,
        )
        svc.commit_lease(spawn, team_id=entity.team_id, member_name="qa")
        control.delete_entity(entity.team_id)
        assert control.get_entity(entity.team_id) is None
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
        assert purge_team_for_deleted_session(session_id="sess-orphan-y") == entity.team_id
        leftover = svc._sqlite().execute("SELECT lease_id FROM team_quota_lease").fetchall()
        assert leftover == []
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        set_team_route_binding_store(None)


def test_purge_require_owner_skips_foreign_occupancy(tmp_path: Path) -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )
    from jiuwenswarm.enterprise.team.control_store import (
        TEAM_STATUS_RUNNING,
        SqliteTeamControlStore,
        set_team_control_store,
    )
    from jiuwenswarm.enterprise.team.session_cleanup import (
        purge_team_for_deleted_session,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(InMemoryTeamRouteBindingStore())
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="owned",
            owner=ident,
            team_session_id="sess-owned",
            status=TEAM_STATUS_RUNNING,
        )
        lease = svc.reserve_team_create(ident, "sess-owned")
        svc.commit_lease(lease, team_id=entity.team_id)
        assert purge_team_for_deleted_session(
            session_id="sess-owned",
            owner_user_id="attacker",
            require_owner=True,
        ) is None
        assert control.get_entity(entity.team_id) is not None
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
        assert purge_team_for_deleted_session(
            session_id="sess-owned",
            owner_user_id="u1",
            require_owner=True,
        ) == entity.team_id
        assert control.get_entity(entity.team_id) is None
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        set_team_route_binding_store(None)


def test_purge_deletes_route_binding_by_team_id(tmp_path: Path) -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )
    from jiuwenswarm.enterprise.team.control_store import (
        TEAM_STATUS_RUNNING,
        SqliteTeamControlStore,
        set_team_control_store,
    )
    from jiuwenswarm.enterprise.team.session_cleanup import (
        purge_team_for_deleted_session,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    routes = InMemoryTeamRouteBindingStore()
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(routes)
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="routed",
            owner=ident,
            team_session_id="sess-routed",
            status=TEAM_STATUS_RUNNING,
        )
        lease = svc.reserve_team_create(ident, "sess-routed")
        svc.commit_lease(lease, team_id=entity.team_id)
        routes.upsert(
            team_session_id="route-other-sid",
            endpoint="http://agent.local",
            team_id=entity.team_id,
            owner=("g1", "b1", "u1"),
        )
        assert purge_team_for_deleted_session(session_id="sess-routed") == entity.team_id
        assert routes.get_by_session("route-other-sid") is None
        assert routes.get_by_team_id(entity.team_id) is None
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        set_team_route_binding_store(None)


def _bind_reconcile_stores(tmp_path: Path):
    from jiuwenswarm.enterprise.team.control_store import SqliteTeamControlStore
    from jiuwenswarm.enterprise.team.route_binding_store import (
        InMemoryTeamRouteBindingStore,
        set_team_route_binding_store,
    )

    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    svc = _svc(tmp_path)
    set_team_control_store(control)
    set_team_quota_service(svc)
    set_team_route_binding_store(InMemoryTeamRouteBindingStore())
    return control, svc


def _unbind_reconcile_stores() -> None:
    from jiuwenswarm.enterprise.team.route_binding_store import set_team_route_binding_store

    set_team_control_store(None)
    set_team_quota_service(None)
    set_team_route_binding_store(None)


def _use_personal_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: volume)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.tenant.tenant_paths.is_enterprise", lambda: False,
    )
    return volume


def test_reconcile_keeps_team_when_session_dir_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_CREATING
    from jiuwenswarm.enterprise.team.session_cleanup import reconcile_teams_with_sessions

    volume = _use_personal_volume(tmp_path, monkeypatch)
    (volume / "agent" / "sessions" / "sess-keep").mkdir(parents=True)
    control, svc = _bind_reconcile_stores(tmp_path)
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="kept",
            owner=ident,
            team_session_id="sess-keep",
            status=TEAM_STATUS_CREATING,
        )
        lease = svc.reserve_team_create(ident, "sess-keep")
        svc.commit_lease(lease, team_id=entity.team_id)
        assert reconcile_teams_with_sessions() == 0
        assert control.get_entity(entity.team_id) is not None
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
    finally:
        _unbind_reconcile_stores()


def test_reconcile_purges_team_when_session_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_CREATING
    from jiuwenswarm.enterprise.team.session_cleanup import reconcile_teams_with_sessions

    volume = _use_personal_volume(tmp_path, monkeypatch)
    (volume / "agent" / "sessions" / "sess-keep").mkdir(parents=True)
    control, svc = _bind_reconcile_stores(tmp_path)
    try:
        ident = _ident()
        other = _ident("u2")
        gone = control.create_entity(
            team_name="gone",
            owner=ident,
            team_session_id="sess-gone",
            status=TEAM_STATUS_CREATING,
        )
        kept = control.create_entity(
            team_name="kept",
            owner=ident,
            team_session_id="sess-keep",
            status=TEAM_STATUS_CREATING,
        )
        foreign = control.create_entity(
            team_name="foreign",
            owner=other,
            team_session_id="sess-foreign",
            status=TEAM_STATUS_CREATING,
        )
        for owner, sid, entity in (
            (ident, "sess-gone", gone),
            (ident, "sess-keep", kept),
            (other, "sess-foreign", foreign),
        ):
            lease = svc.reserve_team_create(owner, sid)
            svc.commit_lease(lease, team_id=entity.team_id)
            spawn = svc.reserve_teammate_spawn(
                team_id=entity.team_id, member_name="qa", owner=owner,
            )
            svc.commit_lease(spawn, team_id=entity.team_id, member_name="qa")
        orphan = svc.reserve_team_create(ident, "sess-lease-only")
        svc.commit_lease(orphan)
        assert reconcile_teams_with_sessions(identity=ident) == 2
        assert control.get_entity(gone.team_id) is None
        assert control.get_entity(kept.team_id) is not None
        assert control.get_entity(foreign.team_id) is not None
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
        assert svc.snapshot_user(other)["teams"]["used"] == 1
        assert reconcile_teams_with_sessions() == 1
        assert control.get_entity(foreign.team_id) is None
        assert svc.snapshot_user(other)["teams"]["used"] == 0
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
    finally:
        _unbind_reconcile_stores()


def test_reconcile_skips_when_data_volume_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_RUNNING
    from jiuwenswarm.enterprise.team.session_cleanup import reconcile_teams_with_sessions

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path / "missing-volume",
    )
    control, svc = _bind_reconcile_stores(tmp_path)
    try:
        ident = _ident()
        entity = control.create_entity(
            team_name="untouched",
            owner=ident,
            team_session_id="sess-offline",
            status=TEAM_STATUS_RUNNING,
        )
        lease = svc.reserve_team_create(ident, "sess-offline")
        svc.commit_lease(lease, team_id=entity.team_id)
        assert reconcile_teams_with_sessions() == 0
        assert control.get_entity(entity.team_id) is not None
        assert svc.snapshot_user(ident)["teams"]["used"] == 1
    finally:
        _unbind_reconcile_stores()


def test_reconcile_purges_when_tenant_sessions_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_CREATING
    from jiuwenswarm.enterprise.team.session_cleanup import reconcile_teams_with_sessions
    from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key

    volume = tmp_path / "vol"
    volume.mkdir()
    ident = _ident()
    key = build_tenant_key(ident.group_id, ident.bot_id, ident.user_id)
    (volume / f"workspace_{key}").mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: volume)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.tenant.tenant_paths.is_enterprise", lambda: True,
    )
    control, svc = _bind_reconcile_stores(tmp_path)
    try:
        entity = control.create_entity(
            team_name="orphan",
            owner=ident,
            team_session_id="sess-wiped",
            status=TEAM_STATUS_CREATING,
        )
        lease = svc.reserve_team_create(ident, "sess-wiped")
        svc.commit_lease(lease, team_id=entity.team_id)
        assert reconcile_teams_with_sessions() == 1
        assert control.get_entity(entity.team_id) is None
        assert svc.snapshot_user(ident)["teams"]["used"] == 0
        from jiuwenswarm.common.utils import get_agent_root_dir
        from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key

        expected = get_agent_root_dir(build_tenant_key(
            ident.group_id, ident.bot_id, ident.user_id,
        )) / "sessions" / "sess-wiped"
        assert not expected.is_dir()
        assert expected == volume / f"workspace_{key}" / "agent" / "sessions" / "sess-wiped"
    finally:
        _unbind_reconcile_stores()


@pytest.mark.asyncio
async def test_list_and_create_wait_for_startup_sweep_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.request_identity import apply_routing_metadata
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods
    from jiuwenswarm.enterprise.team import handlers as team_control
    from jiuwenswarm.enterprise.team import session_cleanup as cleanup
    from jiuwenswarm.enterprise.team.control_store import SqliteTeamControlStore
    from jiuwenswarm.server.context import RequestContext
    from jiuwenswarm.server.transports.ws_sink import WSSink

    ensure_team_control_req_methods()
    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    set_team_control_store(control)
    cleanup.reset_startup_sweep()
    sweeps: list[int] = []

    def _forbid_handler_sweep(**_kwargs: object) -> int:
        sweeps.append(1)
        raise AssertionError("list and create must not sweep")

    monkeypatch.setattr(cleanup, "reconcile_teams_with_sessions", _forbid_handler_sweep)

    def _global(name: str):
        if name == "get_config":
            return lambda: {}
        if name == "resolve_agent_request_mode":
            return lambda mode="team": (mode, mode, mode)
        raise AttributeError(name)

    reserve_calls = {"n": 0}

    async def _stop_after_wait(fn, *args, **kwargs):  # noqa: ANN001
        reserve_calls["n"] += 1
        if reserve_calls["n"] == 1:
            raise RuntimeError("stop-after-wait")
        return None

    monkeypatch.setattr(team_control, "_server_global", _global)
    monkeypatch.setattr(
        "jiuwenswarm.infrastructure.db.team_mysql_actor.run_team_store",
        _stop_after_wait,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: __import__("types").SimpleNamespace(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: __import__("types").SimpleNamespace(),
    )
    release = asyncio.Event()

    async def _sweep() -> None:
        await release.wait()

    task = asyncio.create_task(_sweep())
    cleanup.bind_startup_sweep(task)
    session_id = "sess-wait"
    (tmp_path / session_id).mkdir()
    monkeypatch.setattr(team_control, "get_agent_sessions_dir", lambda: tmp_path)

    class _FakeWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    list_method = getattr(ReqMethod, "TEAM_LIST", ReqMethod.TEAM_CREATE)

    def _ctx(method: ReqMethod, *, with_session: bool) -> tuple[RequestContext, _FakeWS]:
        ws = _FakeWS()
        request = AgentRequest(
            request_id="req-wait",
            session_id=session_id if with_session else "",
            channel_id="web",
            req_method=method,
            params={"session_id": session_id, "description": "wait"} if with_session else {},
            metadata=apply_routing_metadata(
                {}, {"bot_id": "b1", "group_id": "g1", "user_id": "u1"},
            ) if with_session else {},
        )
        return RequestContext(
            request=request,
            sink=WSSink(ws, asyncio.Lock()),
            connection_id="c",
            services=None,  # type: ignore[arg-type]
        ), ws

    try:
        list_ctx, list_ws = _ctx(list_method, with_session=False)
        create_ctx, create_ws = _ctx(ReqMethod.TEAM_CREATE, with_session=True)
        listed = asyncio.create_task(team_control.handle_team_list(list_ctx))
        created = asyncio.create_task(team_control.handle_team_create(create_ctx))
        await asyncio.sleep(0.05)
        assert not listed.done()
        assert not created.done()
        assert sweeps == []
        assert reserve_calls["n"] == 0
        release.set()
        await listed
        await created
        assert list_ws.sent
        assert create_ws.sent
        assert sweeps == []
        assert reserve_calls["n"] >= 1
        await team_control.handle_team_list(list_ctx)
        assert sweeps == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cleanup.reset_startup_sweep()
        set_team_control_store(None)


@pytest.mark.asyncio
async def test_auto_team_name_generation_failure_releases_x_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated-name create is the enterprise create path. A generation failure
    after the X reservation must release the lease."""
    from types import SimpleNamespace

    from jiuwenswarm.agents.harness.team.team_name_generator import TeamNameGenerationError
    from jiuwenswarm.common.request_identity import apply_routing_metadata
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods

    ensure_team_control_req_methods()
    from jiuwenswarm.server.context import RequestContext
    from jiuwenswarm.enterprise.team import handlers as team_control
    from jiuwenswarm.enterprise.team.control_store import SqliteTeamControlStore
    from jiuwenswarm.server.transports.ws_sink import WSSink

    monkeypatch.setenv("TEAM_QUOTA_ENABLED", "1")
    svc = _svc(tmp_path)
    control = SqliteTeamControlStore(tmp_path / "team_control.db")
    set_team_quota_service(svc)
    set_team_control_store(control)
    session_id = "web_fail_name"
    (tmp_path / session_id).mkdir()
    try:
        async def fail_generate(*, description, config_base):
            raise TeamNameGenerationError("team_name generation failed")

        def _global(name: str):
            if name == "get_config":
                return lambda: {}
            if name == "resolve_agent_request_mode":
                return lambda mode="team": (mode, mode, mode)
            raise AttributeError(name)

        monkeypatch.setattr(team_control, "get_agent_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(team_control, "_server_global", _global)
        monkeypatch.setattr(
            "jiuwenswarm.server.handlers.team._create_generated_team_binding",
            fail_generate,
        )
        monkeypatch.setattr(
            "jiuwenswarm.enterprise.team.quota.count_predefined_non_leaders",
            lambda *args, **kwargs: 0,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
            lambda: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
            lambda: SimpleNamespace(),
        )

        request = AgentRequest(
            request_id="req-name-fail",
            session_id=session_id,
            channel_id="web",
            req_method=ReqMethod.TEAM_CREATE,
            params={"mode": "team", "session_id": session_id, "description": "软件开发团队"},
            metadata=apply_routing_metadata(
                {}, {"bot_id": "b1", "group_id": "g1", "user_id": "u1"},
            ),
        )

        class _FakeWS:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, data: str) -> None:
                self.sent.append(data)

        ws = _FakeWS()
        ctx = RequestContext(
            request=request,
            sink=WSSink(ws, asyncio.Lock()),
            connection_id="c",
            services=None,  # type: ignore[arg-type]
        )
        await team_control.handle_team_create(ctx)
        assert ws.sent
        assert svc._list_occupying(KIND_TEAM_CREATE) == []
    finally:
        set_team_control_store(None)
        set_team_quota_service(None)
        control.close()


def test_org_limit_follows_org_policy_not_user_policy(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "wide",
        "priority": 1,
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 4,
    })
    svc.upsert_policy({
        "policy_id": "org",
        "priority": 5,
        "match_expr": "group_id == 'g1'",
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 2,
    })
    svc.upsert_policy({
        "policy_id": "user",
        "priority": 10,
        "match_expr": "user_id == 'u1'",
        "max_teams_per_user": 10,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 1,
    })
    in_org = svc.resolve_limits(_ident("u1", "g1"))
    other_user = svc.resolve_limits(_ident("u2", "g1"))
    other_org = svc.resolve_limits(_ident("u1", "g2"))
    assert in_org.max_teams_per_user == 10
    assert in_org.max_teams_per_org == 2
    assert other_user.max_teams_per_user == 5
    assert other_user.max_teams_per_org == 2
    assert other_org.max_teams_per_org == 4


def test_org_cap_blocks_third_team_and_other_org_is_independent(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "z2",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 2,
    })
    first = svc.reserve_team_create(_ident("u1", "g1"), "s1")
    second = svc.reserve_team_create(_ident("u2", "g1"), "s2")
    svc.commit_lease(first, team_id="t1")
    svc.commit_lease(second, team_id="t2")
    with pytest.raises(TeamQuotaExceeded) as exc:
        svc.reserve_team_create(_ident("u3", "g1"), "s3")
    assert exc.value.kind == KIND_ORG_TEAM_CREATE
    assert exc.value.limit == 2
    assert team_quota_user_message(exc.value) == org_team_create_quota_user_message(2)
    other = svc.reserve_team_create(_ident("u9", "g2"), "s9")
    assert other.status == STATUS_RESERVED
    snap = svc.snapshot_user(_ident("u1", "g1"))
    assert snap["org"] == {"group_id": "g1", "used": 2, "limit": 2}


def test_zero_org_limit_and_empty_group_do_not_cap(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "z0",
        "match_expr": [],
        "max_teams_per_user": 1,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 0,
    })
    held = svc.reserve_team_create(_ident("u1", "g1"), "s1")
    svc.commit_lease(held, team_id="t1")
    with pytest.raises(TeamQuotaExceeded) as exc:
        svc.reserve_team_create(_ident("u1", "g1"), "s2")
    assert exc.value.kind == KIND_TEAM_CREATE
    assert "org" not in svc.snapshot_user(_ident("u1", "g1"))
    no_org = svc.reserve_team_create(_ident("u2", ""), "s-empty")
    assert no_org.status == STATUS_RESERVED
    assert "org" not in svc.snapshot_user(_ident("u2", ""))


def test_release_team_frees_org_slot(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "z1",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 1,
    })
    first = svc.reserve_team_create(_ident("u1", "g1"), "s1")
    svc.commit_lease(first, team_id="t1")
    with pytest.raises(TeamQuotaExceeded):
        svc.reserve_team_create(_ident("u2", "g1"), "s2")
    svc.release_team("t1")
    again = svc.reserve_team_create(_ident("u2", "g1"), "s2")
    assert again.status == STATUS_RESERVED
    usage = svc.list_usage()
    assert usage["orgs"] == [{"group_id": "g1", "used": 1, "limit": 1}]


def test_empty_org_concurrent_create_does_not_oversell(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "z1",
        "match_expr": [],
        "max_teams_per_user": 5,
        "max_teammates_per_team": 5,
        "max_teams_per_org": 1,
    })

    def _one(i: int):
        try:
            return svc.reserve_team_create(_ident(f"u{i}", "g1"), f"sess-{i}")
        except TeamQuotaExceeded as exc:
            return exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one, range(4)))
    ok = [r for r in results if not isinstance(r, TeamQuotaExceeded)]
    denied = [r for r in results if isinstance(r, TeamQuotaExceeded)]
    assert len(ok) == 1
    assert len(denied) == 3
    assert {item.kind for item in denied} == {KIND_ORG_TEAM_CREATE}


@pytest.mark.asyncio
async def test_manager_usage_data_exposes_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager reads ResponseModel.data as the usage result and requires items."""
    import openjiuwen_runtime.foundation.audit as audit

    if not hasattr(audit, "get_audit_manager"):
        audit.get_audit_manager = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    from gateway.enterprise.config.manager_config.routers.quota_routers import list_team_quota_usage

    svc = _svc(tmp_path)
    lease = svc.reserve_team_create(_ident(), "sess-usage")
    svc.commit_lease(lease, team_id="team-usage")
    monkeypatch.setattr(
        "gateway.enterprise.config.manager_config.routers.quota_routers._team_quota_service",
        lambda: svc,
    )
    response = await list_team_quota_usage()
    data = response.data
    assert isinstance(data, dict)
    assert "result" not in data
    assert isinstance(data["items"], list) and data["items"]
    assert data["items"][0]["used"] == 1
    assert data["orgs"] == [{"group_id": "g1", "used": 1, "limit": 0}]


def test_snapshot_counts_session_teams_for_the_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已用 includes the user's live session teams, not only quota leases."""
    import json

    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "org",
        "match_expr": [],
        "max_teams_per_user": 2,
        "max_teams_per_org": 3,
        "max_teammates_per_team": 3,
    })
    ident = _ident()
    root = tmp_path / "home"
    sessions = root / f"workspace_{ident.group_id}::{ident.bot_id}::{ident.user_id}" / "agent" / "sessions"
    live = sessions / "web_live"
    live.mkdir(parents=True)
    (live / "metadata.json").write_text(json.dumps({
        "session_id": "web_live",
        "mode": "team.work.normal",
        "team_name": "jiuwen_team_web_live",
    }), encoding="utf-8")
    chat = sessions / "web_chat"
    chat.mkdir()
    (chat / "metadata.json").write_text(json.dumps({
        "session_id": "web_chat",
        "mode": "agent",
        "team_name": "ignored",
    }), encoding="utf-8")

    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda tenant_key=None: sessions,
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_user_workspace_dir",
        lambda: root,
    )

    snap = svc.snapshot_user(ident)
    assert snap["teams"]["used"] == 1
    assert snap["teams"]["occupying_ids"] == ["jiuwen_team_web_live"]
    assert snap["org"]["used"] == 1

    lease = svc.reserve_team_create(ident, "web_live")
    svc.commit_lease(lease, team_id="tid-live")
    again = svc.snapshot_user(ident)
    assert again["teams"]["used"] == 1
    assert again["org"]["used"] == 1
    assert "tid-live" in again["teams"]["occupying_ids"]


def test_used_counts_every_live_team_of_the_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已用 is the user's live teams, including ones that never took a lease."""
    ident = _ident()
    svc = _svc(tmp_path)
    svc.upsert_policy({
        "policy_id": "one",
        "match_expr": [],
        "max_teams_per_user": 1,
        "max_teams_per_org": 2,
        "max_teammates_per_team": 3,
    })
    sessions = tmp_path / "sessions"
    live = sessions / "sess-live"
    live.mkdir(parents=True)
    (live / "metadata.json").write_text(
        '{"session_id":"sess-live","mode":"team","team_name":"live-team"}',
        encoding="utf-8",
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda tenant_key=None: sessions,
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_user_workspace_dir",
        lambda: tmp_path / "no-such-home",
    )
    store = SqliteTeamControlStore(tmp_path / "teams.db")
    set_team_control_store(store)
    try:
        store.create_entity(
            team_name="live-team",
            owner=ident,
            team_session_id="sess-live",
            status="running",
        )
        store.create_entity(
            team_name="stopped-team",
            owner=ident,
            team_session_id="sess-stopped",
            status="stopped",
        )
        snap = svc.snapshot_user(ident)
        assert snap["teams"]["used"] == 1
        assert snap["teams"]["occupying_ids"] == ["live-team"]
        assert snap["org"]["used"] == 1

        with pytest.raises(TeamQuotaExceeded) as denied:
            svc.reserve_team_create(ident, "sess-new")
        assert denied.value.kind == KIND_TEAM_CREATE
        assert denied.value.used == 1

        same = svc.reserve_team_create(ident, "sess-live")
        assert same.status == STATUS_RESERVED
    finally:
        set_team_control_store(None)
        store.close()


def test_mysql_reserve_counts_extra_teams_before_the_actor_transaction(monkeypatch) -> None:
    """A MySQL reserve must not call the actor again from inside its transaction."""
    import threading

    svc = TeamQuotaService()
    monkeypatch.setattr(svc, "_use_mysql", lambda: True)
    monkeypatch.setattr(svc, "list_policies", lambda: [])

    class _Mappings:
        def first(self):
            return None

        def all(self):
            return []

    class _Result:
        def mappings(self):
            return _Mappings()

    class _Conn:
        async def execute(self, *_args, **_kwargs):
            return _Result()

    class _Begin:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_args):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    class _Handler:
        def get_engine(self):
            return _Engine()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="fake-team-mysql", daemon=True)
    thread.start()

    class _Actor:
        handler = _Handler()

        def call(self, coro_fn):
            if threading.current_thread() is thread:
                raise AssertionError("reentrant actor.call during reserve")
            fut = asyncio.run_coroutine_threadsafe(coro_fn(), loop)
            return fut.result(timeout=2)

    actor = _Actor()
    calls = {"n": 0}
    original_call = actor.call

    def _counting_call(coro_fn):
        calls["n"] += 1
        return original_call(coro_fn)

    actor.call = _counting_call
    monkeypatch.setattr(svc, "_mysql", lambda: actor)
    seen: dict[str, threading.Thread | None] = {"thread": None}

    def _sessions(_identity, _session_id):
        seen["thread"] = threading.current_thread()
        if threading.current_thread() is thread:
            raise AssertionError("session scan ran on the MySQL thread")
        return [], []

    monkeypatch.setattr(svc, "_local_session_pairs", _sessions)
    try:
        lease = svc.reserve_team_create(_ident(), "sess-mysql")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)

    assert lease.status == STATUS_RESERVED
    assert lease.lease_id
    assert calls["n"] == 1
    assert seen["thread"] is not None
    assert seen["thread"] is not thread
