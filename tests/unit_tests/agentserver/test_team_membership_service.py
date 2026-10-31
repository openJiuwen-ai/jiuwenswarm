# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human-member membership service (join / leave / materialize / sender guard)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.enterprise.team import membership as svc
from jiuwenswarm.enterprise.team.control_store import (
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_LEFT,
    MEMBERSHIP_STATUS_PENDING,
    OwnerIdentity,
    SqliteTeamControlStore,
    set_team_control_store,
)

OWNER = OwnerIdentity(user_id="u-owner", group_id="g1", bot_id="b1")
MEMBER = OwnerIdentity(user_id="u-member", group_id="g1", bot_id="b1")
OTHER = OwnerIdentity(user_id="u-other", group_id="g1", bot_id="b1")
STRANGER = OwnerIdentity(user_id="u-x", group_id="g2", bot_id="b1")


class _FakeBackend:
    def __init__(self, *, fail_reason: str | None = None):
        self.spawned: list[tuple[str, str | None]] = []
        self.fail_reason = fail_reason

    async def spawn_human_agent(self, *, member_name, display_name=None, desc=None, prompt=None):
        self.spawned.append((member_name, display_name))
        if self.fail_reason:
            return SimpleNamespace(ok=False, reason=self.fail_reason)
        return SimpleNamespace(ok=True, reason="")


@pytest.fixture
def store(tmp_path):
    s = SqliteTeamControlStore(tmp_path / "team_control.db")
    set_team_control_store(s)
    yield s
    set_team_control_store(None)
    s.close()


@pytest.fixture
def entity(store):
    return store.create_entity(
        team_name="alpha", owner=OWNER, team_session_id="sess-1",
        runtime_team_name="alpha_sess1",
    )


@pytest.fixture
def runtime(monkeypatch):
    """Patch the live backend + DB roster lookups."""
    state = {"backend": None, "roles": {}, "broadcasts": []}

    async def _backend(_name):
        return state["backend"]

    async def _roles(_name):
        return dict(state["roles"])

    monkeypatch.setattr(svc, "_active_team_backend", _backend)
    monkeypatch.setattr(svc, "_db_member_roles", _roles)
    monkeypatch.setattr(
        svc, "_broadcast_membership",
        lambda ch, sid, m, action: state["broadcasts"].append((sid, m.member_name, action)),
    )
    return state


# ---------------------------------------------------------------- validation


def test_default_member_name_is_valid_and_stable():
    name = svc.default_member_name("user@example.com")
    assert name == svc.default_member_name("user@example.com")
    assert svc.validate_member_name(name) == name


@pytest.mark.parametrize("bad", ["", "Human", "human_1", "1human", "人类"])
def test_validate_member_name_rejects(bad):
    with pytest.raises(svc.TeamMembershipError):
        svc.validate_member_name(bad)


# ---------------------------------------------------------------- join


@pytest.mark.asyncio
async def test_join_active_runtime_spawns_seat(store, entity, runtime):
    backend = _FakeBackend()
    runtime["backend"] = backend
    result = await svc.join_team(identity=MEMBER, session_id="sess-1", display_name="Bob")
    assert result.seat_ready and not result.reused_seat
    assert result.membership.status == MEMBERSHIP_STATUS_ACTIVE
    assert backend.spawned == [(result.membership.member_name, "Bob")]
    assert runtime["broadcasts"] == [("sess-1", result.membership.member_name, "joined")]


@pytest.mark.asyncio
async def test_join_idle_runtime_is_pending_then_materialized(store, entity, runtime):
    result = await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")
    assert not result.seat_ready
    assert result.membership.status == MEMBERSHIP_STATUS_PENDING

    backend = _FakeBackend()
    runtime["backend"] = backend
    activated = await svc.materialize_pending_memberships(
        runtime_team_name="alpha_sess1", session_id="sess-1",
    )
    assert [m.member_name for m in activated] == ["human-bob"]
    assert backend.spawned == [("human-bob", None)]
    assert store.get_membership(entity.team_id, "u-member").status == MEMBERSHIP_STATUS_ACTIVE


@pytest.mark.asyncio
async def test_join_reuses_existing_human_seat_without_spawn(store, entity, runtime):
    backend = _FakeBackend()
    runtime["backend"] = backend
    runtime["roles"] = {"human-bob": "human_agent"}
    result = await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")
    assert result.seat_ready and result.reused_seat
    assert backend.spawned == []


@pytest.mark.asyncio
async def test_join_rejects_seat_owned_by_ai_member(store, entity, runtime):
    runtime["backend"] = _FakeBackend()
    runtime["roles"] = {"backend-dev": "teammate"}
    with pytest.raises(svc.TeamMembershipError) as exc:
        await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="backend-dev")
    assert exc.value.code == "CONFLICT"


@pytest.mark.asyncio
async def test_join_rejects_seat_taken_by_other_user(store, entity, runtime):
    runtime["backend"] = _FakeBackend()
    await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")
    with pytest.raises(svc.TeamMembershipError) as exc:
        await svc.join_team(identity=OTHER, session_id="sess-1", member_name="human-bob")
    assert exc.value.code == "CONFLICT"


@pytest.mark.asyncio
async def test_join_is_idempotent_for_same_user(store, entity, runtime):
    runtime["backend"] = _FakeBackend()
    first = await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")
    again = await svc.join_team(identity=MEMBER, session_id="sess-1")
    assert again.membership.membership_id == first.membership.membership_id
    assert again.membership.member_name == "human-bob"
    with pytest.raises(svc.TeamMembershipError):
        await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-other")


@pytest.mark.asyncio
async def test_join_cross_scope_forbidden(store, entity, runtime):
    with pytest.raises(svc.TeamMembershipError) as exc:
        await svc.join_team(identity=STRANGER, session_id="sess-1")
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_join_unknown_team(store, runtime):
    with pytest.raises(svc.TeamMembershipError) as exc:
        await svc.join_team(identity=MEMBER, session_id="missing")
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_join_spawn_failure_surfaces(store, entity, runtime):
    runtime["backend"] = _FakeBackend(fail_reason="HITT capability is disabled")
    with pytest.raises(svc.TeamMembershipError) as exc:
        await svc.join_team(identity=MEMBER, session_id="sess-1")
    assert exc.value.code == "SPAWN_FAILED"


# ---------------------------------------------------------------- leave


@pytest.mark.asyncio
async def test_leave_marks_left_and_blocks_repeat(store, entity, runtime):
    runtime["backend"] = _FakeBackend()
    await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")
    left = await svc.leave_team(identity=MEMBER, session_id="sess-1")
    assert left.status == MEMBERSHIP_STATUS_LEFT
    assert runtime["broadcasts"][-1] == ("sess-1", "human-bob", "left")
    with pytest.raises(svc.TeamMembershipError):
        await svc.leave_team(identity=MEMBER, session_id="sess-1")


# ---------------------------------------------------------------- sender guard


@pytest.mark.asyncio
async def test_resolve_sender_member_name_guard(store, entity, runtime):
    runtime["backend"] = _FakeBackend()
    await svc.join_team(identity=MEMBER, session_id="sess-1", member_name="human-bob")

    # legit seat
    assert svc.resolve_sender_member_name(session_id="sess-1", user_id="u-member") == "human-bob"
    # impersonation attempt → own seat
    assert svc.resolve_sender_member_name(
        session_id="sess-1", user_id="u-member", requested_member_name="human-eve",
    ) == "human-bob"
    # owner without membership → GodView
    assert svc.resolve_sender_member_name(
        session_id="sess-1", user_id="u-owner", requested_member_name="human-bob",
    ) == ""
    # left member → dropped
    await svc.leave_team(identity=MEMBER, session_id="sess-1")
    assert svc.resolve_sender_member_name(session_id="sess-1", user_id="u-member") == ""
