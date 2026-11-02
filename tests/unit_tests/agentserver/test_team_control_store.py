# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise Team control store: TeamEntity / TeamMembership (SQLite)."""

from __future__ import annotations

import pytest

from jiuwenswarm.enterprise.team.control_store import (
    DEFAULT_HUMAN_PERMISSIONS,
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_LEFT,
    MEMBERSHIP_STATUS_PENDING,
    TEAM_STATUS_CREATING,
    TEAM_STATUS_DELETED,
    TEAM_STATUS_RUNNING,
    OwnerIdentity,
    SqliteTeamControlStore,
    TeamControlStoreError,
)


@pytest.fixture
def store(tmp_path):
    s = SqliteTeamControlStore(tmp_path / "team_control.db")
    yield s
    s.close()


OWNER = OwnerIdentity(user_id="u-owner", group_id="g1", bot_id="b1")
MEMBER = OwnerIdentity(user_id="u-member", group_id="g1", bot_id="b1")
STRANGER = OwnerIdentity(user_id="u-x", group_id="g2", bot_id="b1")


def test_create_and_lookup_entity(store):
    entity = store.create_entity(
        team_name="alpha", owner=OWNER, template_id="jiuwen_team",
        team_session_id="sess-1", runtime_team_name="alpha_sess1",
    )
    assert entity.status == TEAM_STATUS_CREATING
    assert entity.runtime_mode == "local"
    assert len(entity.team_id) == 32

    assert store.get_entity(entity.team_id) == entity
    assert store.get_entity_by_name("alpha") == entity
    assert store.get_entity_by_session("sess-1") == entity
    assert store.get_entity("nope") is None


def test_team_name_unique(store):
    store.create_entity(team_name="alpha", owner=OWNER)
    with pytest.raises(TeamControlStoreError) as exc:
        store.create_entity(team_name="alpha", owner=MEMBER)
    assert exc.value.code == "CONFLICT"


def test_status_transitions_and_delete_filter(store):
    entity = store.create_entity(team_name="alpha", owner=OWNER)
    updated = store.set_status(entity.team_id, TEAM_STATUS_RUNNING)
    assert updated is not None and updated.status == TEAM_STATUS_RUNNING
    assert updated.updated_at >= entity.updated_at

    by_name = store.set_status_by_name("alpha", TEAM_STATUS_DELETED)
    assert by_name is not None and by_name.status == TEAM_STATUS_DELETED
    assert store.list_entities(owner=OWNER) == []
    assert len(store.list_entities(owner=OWNER, include_deleted=True)) == 1

    with pytest.raises(TeamControlStoreError):
        store.set_status(entity.team_id, "bogus")


def test_list_entities_by_owner(store):
    store.create_entity(team_name="a", owner=OWNER)
    store.create_entity(team_name="b", owner=OWNER)
    store.create_entity(team_name="c", owner=STRANGER)
    assert {e.team_name for e in store.list_entities(owner=OWNER)} == {"a", "b"}
    assert {e.team_name for e in store.list_entities()} == {"a", "b", "c"}


def test_update_entity_rejects_unknown_fields(store):
    entity = store.create_entity(team_name="alpha", owner=OWNER)
    with pytest.raises(TeamControlStoreError):
        store.update_entity(entity.team_id, owner_user_id="hijack")
    updated = store.update_entity(
        entity.team_id, team_session_id="sess-9", extra={"k": "v"},
    )
    assert updated.team_session_id == "sess-9"
    assert updated.extra == {"k": "v"}


def test_membership_upsert_and_seat_uniqueness(store):
    entity = store.create_entity(team_name="alpha", owner=OWNER)
    m1 = store.upsert_membership(
        team_id=entity.team_id, identity=MEMBER, member_name="human-aaa",
        status=MEMBERSHIP_STATUS_PENDING,
    )
    assert m1.permissions == DEFAULT_HUMAN_PERMISSIONS
    assert m1.status == MEMBERSHIP_STATUS_PENDING
    assert not m1.is_active

    # same user → update in place (same membership_id)
    m2 = store.upsert_membership(
        team_id=entity.team_id, identity=MEMBER, member_name="human-aaa",
        status=MEMBERSHIP_STATUS_ACTIVE, display_name="Bob",
    )
    assert m2.membership_id == m1.membership_id
    assert m2.is_active and m2.display_name == "Bob"

    # another user cannot take the same seat
    with pytest.raises(TeamControlStoreError) as exc:
        store.upsert_membership(
            team_id=entity.team_id, identity=STRANGER, member_name="human-aaa",
        )
    assert exc.value.code == "CONFLICT"

    assert store.get_membership(entity.team_id, "u-member").member_name == "human-aaa"
    assert store.get_membership_by_member_name(entity.team_id, "human-aaa").identity == MEMBER
    assert store.get_membership(entity.team_id, "u-x") is None


def test_membership_status_and_listing(store):
    entity = store.create_entity(team_name="alpha", owner=OWNER)
    a = store.upsert_membership(team_id=entity.team_id, identity=MEMBER, member_name="human-a",
                                status=MEMBERSHIP_STATUS_ACTIVE)
    b = store.upsert_membership(team_id=entity.team_id, identity=STRANGER, member_name="human-b")
    assert [m.member_name for m in store.list_memberships(entity.team_id)] == ["human-a", "human-b"]
    assert [m.member_name for m in store.list_memberships(
        entity.team_id, statuses=[MEMBERSHIP_STATUS_PENDING])] == ["human-b"]

    left = store.set_membership_status(a.membership_id, MEMBERSHIP_STATUS_LEFT)
    assert left.status == MEMBERSHIP_STATUS_LEFT
    assert store.set_membership_status("missing", MEMBERSHIP_STATUS_LEFT) is None
    with pytest.raises(TeamControlStoreError):
        store.set_membership_status(b.membership_id, "nope")


def test_delete_entity_cascades_memberships(store):
    entity = store.create_entity(team_name="alpha", owner=OWNER)
    store.upsert_membership(team_id=entity.team_id, identity=MEMBER, member_name="human-a")
    assert store.delete_entity(entity.team_id) is True
    assert store.get_entity(entity.team_id) is None
    assert store.list_memberships(entity.team_id) == []
    assert store.delete_entity(entity.team_id) is False


def test_owner_identity_scope():
    assert OWNER.same_scope(MEMBER)
    assert not OWNER.same_scope(STRANGER)
    assert OWNER.as_dict() == {"user_id": "u-owner", "group_id": "g1", "bot_id": "b1"}


def _mysql_settings_env(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_DB_PORT", "3306")
    monkeypatch.setenv("GATEWAY_DB_USER", "u")
    monkeypatch.setenv("GATEWAY_DB_PASSWORD", "p")
    monkeypatch.setenv("GATEWAY_DB_NAME", "db")


def test_use_team_mysql_true_for_mysql(monkeypatch):
    import jiuwenswarm.infrastructure.db.team_mysql_actor as actor

    monkeypatch.setenv("GATEWAY_DB_TYPE", "mysql")
    _mysql_settings_env(monkeypatch)
    assert actor.use_team_mysql() is True


def test_run_team_store_sqlite_stays_on_the_caller_thread(monkeypatch):
    import asyncio
    import threading

    import jiuwenswarm.infrastructure.db.team_mysql_actor as actor

    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    monkeypatch.setenv("GATEWAY_SQLITE_PATH", "gateway.db")

    async def _go():
        caller = threading.get_ident()
        seen = {}

        def _fn():
            seen["ident"] = threading.get_ident()
            return 7

        assert await actor.run_team_store(_fn) == 7
        assert seen["ident"] == caller

    asyncio.run(_go())


def test_run_team_store_mysql_waits_off_the_event_loop(monkeypatch):
    import asyncio
    import threading

    import jiuwenswarm.infrastructure.db.team_mysql_actor as actor

    monkeypatch.setenv("GATEWAY_DB_TYPE", "mysql")
    _mysql_settings_env(monkeypatch)

    async def _go():
        caller = threading.get_ident()
        seen = {}
        started = threading.Event()
        loop_ran = asyncio.Event()

        def _fn():
            seen["ident"] = threading.get_ident()
            started.set()
            threading.Event().wait(0.2)
            return 3

        async def _health():
            while not started.is_set():
                await asyncio.sleep(0.01)
            loop_ran.set()

        task = asyncio.create_task(_health())
        assert await actor.run_team_store(_fn) == 3
        await task
        assert loop_ran.is_set()
        assert seen["ident"] != caller

    asyncio.run(_go())


def test_use_team_mysql_false_for_sqlite(monkeypatch):
    import jiuwenswarm.infrastructure.db.team_mysql_actor as actor

    monkeypatch.setenv("GATEWAY_DB_TYPE", "sqlite")
    monkeypatch.setenv("GATEWAY_SQLITE_PATH", "gateway.db")
    assert actor.use_team_mysql() is False


def test_use_team_mysql_false_for_postgresql(monkeypatch):
    import jiuwenswarm.infrastructure.db.team_mysql_actor as actor

    _mysql_settings_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_DB_PORT", "5432")
    for kind in ("postgresql", "postgres", "pg"):
        monkeypatch.setenv("GATEWAY_DB_TYPE", kind)
        assert actor.use_team_mysql() is False


def test_unique_violation_detects_mysql():
    from jiuwenswarm.infrastructure.db.team_mysql_actor import is_unique_violation

    class _Orig:
        def __init__(self, *args):
            self.args = args

        def __str__(self):
            return str(self.args)

    class _Wrap(Exception):
        def __init__(self, orig):
            super().__init__("wrapped")
            self.orig = orig

    assert is_unique_violation(_Wrap(_Orig(1062, "Duplicate entry 'x'")))
    assert not is_unique_violation(_Wrap(_Orig(1054, "Unknown column")))


def test_team_mysql_actor_call_rejects_reentry():
    import asyncio
    import threading

    from jiuwenswarm.infrastructure.db.team_mysql_actor import TeamMysqlActor

    actor = TeamMysqlActor()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=actor._run_loop,
        args=(loop,),
        name="team-mysql-reentry",
        daemon=True,
    )
    actor._loop = loop
    actor._thread = thread
    actor._ready = True
    thread.start()

    async def _nested():
        async def _ok():
            return 1

        actor.call(lambda: _ok())

    try:
        with pytest.raises(RuntimeError, match="cannot be reentered"):
            actor.call(lambda: _nested())
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_gateway_table_init_does_not_create_team_tables():
    from gateway.config.enterprise.tables.table_init import (
        ALL_TABLE_DEFINITIONS,
    )

    names = {table.table_name for table in ALL_TABLE_DEFINITIONS}
    assert "team_entity" not in names
    assert "team_quota_lease" not in names


def test_team_mysql_actor_call_timeout_has_a_message():
    import asyncio
    import threading

    from jiuwenswarm.infrastructure.db.team_mysql_actor import TeamMysqlActor

    actor = TeamMysqlActor()
    actor._call_timeout = 0.05
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=actor._run_loop,
        args=(loop,),
        name="team-mysql-timeout",
        daemon=True,
    )
    actor._loop = loop
    actor._thread = thread
    actor._ready = True
    thread.start()

    async def _hang():
        await asyncio.Event().wait()

    try:
        with pytest.raises(TimeoutError, match="timed out after"):
            actor.call(lambda: _hang())
    finally:
        def _stop():
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.stop()

        loop.call_soon_threadsafe(_stop)
        thread.join(timeout=2)


def test_team_mysql_actor_call_reconnects_when_cancel_does_not_finish():
    import asyncio
    import threading

    from jiuwenswarm.infrastructure.db.team_mysql_actor import TeamMysqlActor

    actor = TeamMysqlActor()
    actor._call_timeout = 0.05
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=actor._run_loop,
        args=(loop,),
        name="team-mysql-reconnect",
        daemon=True,
    )
    actor._loop = loop
    actor._thread = thread
    actor._ready = True
    replaced = threading.Event()

    async def _replace():
        replaced.set()

    actor._replace_handler = _replace  # type: ignore[method-assign]
    thread.start()

    async def _block_the_thread():
        # A synchronous sleep does not observe cancellation, so the call
        # has to drop the connection instead of waiting on this coroutine.
        import time

        time.sleep(1.5)

    try:
        with pytest.raises(TimeoutError, match="timed out after"):
            actor.call(lambda: _block_the_thread())
        assert replaced.wait(timeout=2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
