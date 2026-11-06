# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live Gateway MySQL tests for team_entity / team_membership / team_route_binding.

Default skip. Run::

    TEAM_CONTROL_MYSQL_TEST=1 python3 -m pytest -m mysql \\
      tests/unit_tests/agentserver/test_team_control_mysql.py --no-cov -q

Rows use prefix ``_ut_team_`` and are deleted in teardown.
"""

from __future__ import annotations

import os
import uuid

import pytest

from jiuwenswarm.enterprise.team.control_store import (
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_PENDING,
    TEAM_STATUS_DELETED,
    TEAM_STATUS_RUNNING,
    OwnerIdentity,
    TeamControlStoreError,
)

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TEAM_CONTROL_MYSQL_TEST", "").strip() != "1",
        reason="set TEAM_CONTROL_MYSQL_TEST=1 to hit GATEWAY_DB_*",
    ),
]

OWNER = OwnerIdentity(user_id="u-owner", group_id="g1", bot_id="b1")
MEMBER = OwnerIdentity(user_id="u-member", group_id="g1", bot_id="b1")
STRANGER = OwnerIdentity(user_id="u-x", group_id="g2", bot_id="b1")


def _sql(sql: str, params: dict | None = None):
    from jiuwenswarm.infrastructure.db.team_mysql_actor import fetch_all, get_team_mysql_actor

    actor = get_team_mysql_actor()

    async def _op():
        return await fetch_all(actor.handler, sql, params or {})

    return actor.call(_op)


@pytest.fixture(scope="module")
def mysql_stores():
    """One actor for the module: re-init_table on a new handler raises SAWarning (error)."""
    from jiuwenswarm.enterprise.team.route_binding_mysql import MysqlTeamRouteBindingStore
    from jiuwenswarm.infrastructure.db.team_mysql_actor import reset_team_mysql_actor
    from jiuwenswarm.enterprise.team.control_mysql import MysqlTeamControlStore
    from jiuwenswarm.enterprise.team.control_store import set_team_control_store

    reset_team_mysql_actor()
    control = MysqlTeamControlStore()
    routes = MysqlTeamRouteBindingStore()
    set_team_control_store(control)
    yield control, routes
    set_team_control_store(None)
    reset_team_mysql_actor()


@pytest.fixture
def stores(mysql_stores):
    control, routes = mysql_stores
    prefix = f"_ut_team_{uuid.uuid4().hex[:12]}"
    yield control, routes, prefix
    for entity in control.list_entities(include_deleted=True):
        if entity.team_name.startswith("_ut_team_") or str(entity.team_id).startswith("_ut_"):
            control.delete_entity(entity.team_id)
    for binding in routes.list_all():
        if str(binding.team_session_id).startswith("_ut_"):
            routes.delete(binding.team_session_id)


def test_entity_persists_in_mysql_table(stores):
    control, _routes, prefix = stores
    entity = control.create_entity(
        team_name=f"{prefix}_alpha",
        owner=OWNER,
        team_id=f"{prefix}e1",
        team_session_id=f"{prefix}_sess",
        extra={"k": "v"},
    )
    rows = _sql(
        "SELECT team_name, owner_user_id, owner_group_id, owner_bot_id, extra "
        "FROM team_entity WHERE team_id = :tid",
        {"tid": entity.team_id},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["team_name"] == f"{prefix}_alpha"
    assert row["owner_user_id"] == "u-owner"
    assert row["owner_group_id"] == "g1"
    extra = row["extra"]
    if isinstance(extra, str):
        import json

        extra = json.loads(extra)
    assert extra == {"k": "v"}
    assert control.get_entity_by_name(f"{prefix}_alpha") == entity
    assert control.get_entity_by_session(f"{prefix}_sess").team_id == entity.team_id


def test_team_name_unique_and_owner_list(stores):
    control, _routes, prefix = stores
    control.create_entity(team_name=f"{prefix}_a", owner=OWNER)
    control.create_entity(team_name=f"{prefix}_b", owner=OWNER)
    control.create_entity(team_name=f"{prefix}_c", owner=STRANGER)
    with pytest.raises(TeamControlStoreError) as exc:
        control.create_entity(team_name=f"{prefix}_a", owner=MEMBER)
    assert exc.value.code == "CONFLICT"
    owned = {e.team_name for e in control.list_entities(owner=OWNER)}
    assert owned == {f"{prefix}_a", f"{prefix}_b"}


def test_status_and_delete_hides_then_sql_gone(stores):
    control, _routes, prefix = stores
    entity = control.create_entity(team_name=f"{prefix}_alpha", owner=OWNER)
    updated = control.set_status(entity.team_id, TEAM_STATUS_RUNNING)
    assert updated is not None and updated.status == TEAM_STATUS_RUNNING
    control.set_status_by_name(f"{prefix}_alpha", TEAM_STATUS_DELETED)
    assert control.list_entities(owner=OWNER) == []
    assert len(control.list_entities(owner=OWNER, include_deleted=True)) == 1
    control.upsert_membership(
        team_id=entity.team_id, identity=MEMBER, member_name="human-a"
    )
    assert control.delete_entity(entity.team_id) is True
    assert _sql(
        "SELECT team_id FROM team_entity WHERE team_id = :tid",
        {"tid": entity.team_id},
    ) == []
    assert _sql(
        "SELECT membership_id FROM team_membership WHERE team_id = :tid",
        {"tid": entity.team_id},
    ) == []


def test_membership_unique_and_sql(stores):
    control, _routes, prefix = stores
    entity = control.create_entity(team_name=f"{prefix}_alpha", owner=OWNER)
    m1 = control.upsert_membership(
        team_id=entity.team_id,
        identity=MEMBER,
        member_name="human-aaa",
        status=MEMBERSHIP_STATUS_PENDING,
    )
    m2 = control.upsert_membership(
        team_id=entity.team_id,
        identity=MEMBER,
        member_name="human-aaa",
        display_name="Bob",
        status=MEMBERSHIP_STATUS_ACTIVE,
    )
    assert m2.membership_id == m1.membership_id
    with pytest.raises(TeamControlStoreError) as taken:
        control.upsert_membership(
            team_id=entity.team_id, identity=STRANGER, member_name="human-aaa"
        )
    assert taken.value.code == "CONFLICT"
    rows = _sql(
        "SELECT user_id, member_name, display_name, status "
        "FROM team_membership WHERE team_id = :tid",
        {"tid": entity.team_id},
    )
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u-member"
    assert rows[0]["display_name"] == "Bob"
    assert rows[0]["status"] == MEMBERSHIP_STATUS_ACTIVE


def test_route_version_owner_write_once_and_latest_team_id(stores):
    _control, routes, prefix = stores
    sid = f"{prefix}_sess"
    b1 = routes.upsert(
        team_session_id=sid,
        endpoint="http://10.0.0.1:8086",
        agent_service_id="pod-1",
        owner=("g1", "b1", "u1"),
        team_id=f"{prefix}t",
    )
    assert b1.route_version == 1
    same = routes.upsert(
        team_session_id=sid,
        endpoint="http://10.0.0.1:8086",
        agent_service_id="pod-1",
        owner=("g9", "b9", "u9"),
    )
    assert same.route_version == 1
    assert same.owner_triple() == ("g1", "b1", "u1")
    bumped = routes.upsert(
        team_session_id=sid,
        endpoint="http://10.0.0.2:8086",
        agent_service_id="pod-2",
    )
    assert bumped.route_version == 2
    sid2 = f"{prefix}_sess2"
    routes.upsert(
        team_session_id=sid2,
        endpoint="http://10.0.0.3:8086",
        team_id=f"{prefix}t",
    )
    latest = routes.get_by_team_id(f"{prefix}t")
    assert latest is not None and latest.team_session_id == sid2
    rows = _sql(
        "SELECT team_session_id, route_version, owner_user_id "
        "FROM team_route_binding WHERE team_session_id = :sid",
        {"sid": sid},
    )
    assert rows[0]["route_version"] == 2
    assert rows[0]["owner_user_id"] == "u1"
