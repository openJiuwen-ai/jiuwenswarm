# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``max_teammates`` / Y-quota guard wrapping ``TeamBackend.spawn_member``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.schema.team import MemberOpResult, TeamRole

from jiuwenswarm.enterprise.team import member_limits
from jiuwenswarm.enterprise.team.quota import TeamQuotaExceeded


@pytest.fixture(autouse=True)
def _prefer_english_i18n(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helpers call ``t()`` without language; pin resolver so CI/dev yaml zh 不干扰。"""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.preferred_language.resolve_preferred_language",
        lambda: "en",
    )


class _Roster:
    def __init__(self, rows):
        self.rows = rows
        self.roles = {}

    async def get_team_members(self, team_name):
        return [SimpleNamespace(member_name=name, role=self.roles.get(name, "teammate"))
                for name, _display, _status in self.rows]

    async def get_member_roster(self, team_name):
        return list(self.rows)


class _Backend:
    def __init__(self, rows):
        self.team_name = "alpha"
        self.leader_member_name = "team-leader"
        self.db = SimpleNamespace(member=_Roster(rows))
        self.calls: list[str] = []
        self.shutdowns: list[str] = []

    async def spawn_member(self, member_name, *args, **kwargs):
        self.calls.append(member_name)
        return MemberOpResult.success()

    async def shutdown_member(self, member_name, *args, **kwargs):
        self.shutdowns.append(member_name)
        return MemberOpResult.success()


def _leader(rows):
    backend = _Backend(rows)
    agent = SimpleNamespace(role=TeamRole.LEADER, team_backend=backend)
    return agent, backend


def test_resolve_max_teammates_from_template_then_default():
    cfg = {"modes": {"team": {"t1": {"max_teammates": 3}, "t2": {}}}}
    assert member_limits.resolve_max_teammates(cfg, "t1") == 3
    assert member_limits.resolve_max_teammates(cfg, "t2") == 3  # falls back to any team
    assert member_limits.resolve_max_teammates({}, None) == member_limits.DEFAULT_MAX_TEAMMATES
    assert member_limits.resolve_max_teammates({"team": {"max_teammates": "7"}}) == 7
    assert member_limits.resolve_max_teammates({"team": {"max_teammates": 0}}) == member_limits.DEFAULT_MAX_TEAMMATES


@pytest.mark.asyncio
async def test_guard_allows_under_limit_and_blocks_at_limit():
    rows = [("team-leader", "Leader", "ready"), ("a", "A", "ready")]
    agent, backend = _leader(rows)
    assert member_limits.attach_teammate_limit_guard(agent, max_teammates=3)

    ok = await backend.spawn_member("b", "B", object())
    assert ok.ok and backend.calls == ["b"]

    rows.append(("b", "B", "ready"))
    blocked = await backend.spawn_member("c", "C", object())
    assert not blocked.ok
    assert blocked.reason.startswith(member_limits.TEAM_MEMBER_QUOTA_EXCEEDED)
    assert "at most 3 members including the Leader" in blocked.reason
    assert backend.calls == ["b"]  # original not reached


def test_teammate_quota_user_message_includes_leader():
    from jiuwenswarm.common.i18n import t

    assert member_limits.teammate_quota_user_message(3) == t(
        "enterprise.team.teammate_full", language="en", limit=3
    )
    assert t("enterprise.team.teammate_full", language="zh", limit=3) == (
        "人数已满：该团队最多3人（含 Leader），无法再添加队员"
    )
    assert member_limits.teammate_quota_fail_reason(5).startswith(
        member_limits.TEAM_MEMBER_QUOTA_EXCEEDED
    )


@pytest.mark.asyncio
async def test_guard_quota_exceeded_callback_fires():
    rows = [("team-leader", "Leader", "ready"), ("a", "A", "ready")]
    agent, backend = _leader(rows)
    notices: list[dict] = []
    member_limits.attach_teammate_limit_guard(
        agent,
        max_teammates=2,
        on_quota_exceeded=lambda **kwargs: notices.append(kwargs),
    )
    blocked = await backend.spawn_member("b", "B", object())
    assert not blocked.ok
    assert notices == [{"used": 2, "limit": 2, "member_name": "b"}]


@pytest.mark.asyncio
async def test_guard_n_includes_leader():
    rows = [("team-leader", "Leader", "ready")]
    agent, backend = _leader(rows)
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1)
    blocked = await backend.spawn_member("a", "A", object())
    assert not blocked.ok
    assert "at most 1 members including the Leader" in blocked.reason

    agent2, backend2 = _leader(rows)
    member_limits.attach_teammate_limit_guard(agent2, max_teammates=2)
    assert (await backend2.spawn_member("a", "A", object())).ok


@pytest.mark.asyncio
async def test_guard_skips_leader_role_rows():
    rows = [("team-leader", "Leader", "ready"), ("a", "A", "ready")]
    agent, backend = _leader(rows)
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1)
    res = await backend.spawn_member("team-leader", "L", object(), role=TeamRole.LEADER)
    assert res.ok


@pytest.mark.asyncio
async def test_guard_skips_leader_name_even_without_leader_role():
    """build_team calls spawn_member(team-leader) with default role=TEAMMATE."""
    rows = [("team-leader", "Leader", "ready")]
    agent, backend = _leader(rows)
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1)
    res = await backend.spawn_member("team-leader", "L", object())
    assert res.ok
    assert backend.calls == ["team-leader"]
    blocked = await backend.spawn_member("frontend-dev", "F", object())
    assert not blocked.ok


def test_guard_is_idempotent_and_leader_only():
    agent, backend = _leader([])
    assert member_limits.attach_teammate_limit_guard(agent, max_teammates=5)
    first = backend.spawn_member
    assert member_limits.attach_teammate_limit_guard(agent, max_teammates=5)
    assert backend.spawn_member is first

    teammate = SimpleNamespace(role=TeamRole.TEAMMATE, team_backend=_Backend([]))
    assert not member_limits.attach_teammate_limit_guard(teammate, max_teammates=5)
    fresh, _ = _leader([])
    assert not member_limits.attach_teammate_limit_guard(fresh, max_teammates=0)


@pytest.mark.asyncio
async def test_detach_restores_original():
    agent, backend = _leader([("team-leader", "L", "ready"), ("a", "A", "ready")])
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1)
    assert not (await backend.spawn_member("b", "B", object())).ok
    member_limits.detach_teammate_limit_guard(agent)
    assert (await backend.spawn_member("b", "B", object())).ok


class _Lease:
    noop = False
    lease_id = "lease-1"


class _QuotaSvc:
    def __init__(self, *, fail: bool = False, limit: int = 5):
        self.fail = fail
        self.limit = limit
        self.reserved: list[dict] = []
        self.committed: list[tuple] = []
        self.released: list = []
        self.teammates_released: list[tuple[str, str]] = []

    def reserve_teammate_spawn(self, **kwargs):
        self.reserved.append(kwargs)
        if self.fail:
            raise TeamQuotaExceeded(
                "team quota exceeded",
                kind="teammate_spawn",
                used=self.limit,
                limit=self.limit,
                default=self.limit,
            )
        return _Lease()

    def commit_lease(self, lease, **kwargs):
        self.committed.append((lease, kwargs))

    def release_lease(self, lease):
        self.released.append(lease)

    def release_teammate(self, team_id: str, member_name: str):
        self.teammates_released.append((team_id, member_name))


@pytest.mark.asyncio
async def test_guard_reserves_teammate_spawn_lease(monkeypatch):
    svc = _QuotaSvc()
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_team_quota_service",
        lambda: svc,
    )
    rows = [("team-leader", "Leader", "ready")]
    agent, backend = _leader(rows)
    owner = object()
    assert member_limits.attach_teammate_limit_guard(agent, team_id="tid-1", owner=owner)
    ok = await backend.spawn_member("frontend-dev", "F", object())
    assert ok.ok
    assert svc.reserved[0]["team_id"] == "tid-1"
    assert svc.reserved[0]["member_name"] == "frontend-dev"
    assert svc.reserved[0]["owner"] is owner
    assert svc.committed
    assert not svc.released


@pytest.mark.asyncio
async def test_guard_lease_quota_exceeded_blocks_spawn(monkeypatch):
    svc = _QuotaSvc(fail=True, limit=5)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_team_quota_service",
        lambda: svc,
    )
    notices: list[dict] = []
    agent, backend = _leader([("team-leader", "Leader", "ready")])
    member_limits.attach_teammate_limit_guard(
        agent,
        team_id="tid-full",
        owner=object(),
        on_quota_exceeded=lambda **kwargs: notices.append(kwargs),
    )
    blocked = await backend.spawn_member("qa-engineer", "Q", object())
    assert not blocked.ok
    assert blocked.reason.startswith(member_limits.TEAM_MEMBER_QUOTA_EXCEEDED)
    assert "at most 5 members including the Leader" in blocked.reason
    assert backend.calls == []
    assert notices == [{"used": 5, "limit": 5, "member_name": "qa-engineer"}]


@pytest.mark.asyncio
async def test_shutdown_releases_teammate_lease(monkeypatch):
    svc = _QuotaSvc()
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.quota.get_team_quota_service",
        lambda: svc,
    )
    agent, backend = _leader([("team-leader", "Leader", "ready"), ("a", "A", "ready")])
    member_limits.attach_teammate_limit_guard(agent, team_id="tid-1", owner=object())
    ok = await backend.shutdown_member("a")
    assert ok.ok
    assert svc.teammates_released == [("tid-1", "a")]


def test_explicit_team_size_blocks_only_a_single_over_limit_reading():
    assert member_limits.requested_team_size_over_limit("请你帮我创建一个四人团队", 3) == 4
    assert member_limits.requested_team_size_over_limit("三人团队", 3) is None
    assert member_limits.requested_team_size_over_limit("帮我创建团队", 3) is None
    assert member_limits.requested_team_size_over_limit("3到5人团队", 3) is None
    assert member_limits.requested_team_size_over_limit("至少4人团队", 3) is None
    assert member_limits.requested_team_size_over_limit("三个队员", 3) == 4
    assert member_limits.requested_team_size_over_limit("两名队员", 3) is None
    assert member_limits.requested_team_size_over_limit("四人团队，两个队员", 3) is None
    assert member_limits.requested_team_size_over_limit("team of 4", 3) == 4
    assert member_limits.requested_team_size_over_limit("a 4-person team", 3) == 4
    assert member_limits.requested_team_size_over_limit("4 members", 5) is None
    assert member_limits.requested_team_size_over_limit("", 3) is None
    assert member_limits.requested_team_size_over_limit("包含三个成员", 3) is None
    assert member_limits.requested_team_size_over_limit("九十九人团队", 5) == 99
    assert member_limits.explicit_whole_team_size("两个人的团队") == 2
    assert member_limits.explicit_whole_team_size("再加 4 人") is None
    assert member_limits.explicit_whole_team_size("除我以外 3 人") is None
    assert member_limits.explicit_whole_team_size("再加 4 人", enterprise=True) == 5
    assert member_limits.explicit_whole_team_size("除我以外三人", enterprise=True) == 4
    assert member_limits.requested_team_size_over_limit("再加 4 人", 5) is None
    assert member_limits.requested_team_size_over_limit("再加 4 人", 5, enterprise=True) is None
    assert member_limits.requested_team_size_over_limit("再加 5 人", 5, enterprise=True) == 6
    from jiuwenswarm.common.i18n import t

    note = member_limits.enterprise_recruit_constraint("五人团队")
    assert "5 members including the leader" in note
    assert "recruit 4 more teammates" in note
    assert "sidebar list omits the leader" in note
    exclude = member_limits.enterprise_recruit_constraint("三个专家")
    assert "4 members including the leader" in exclude
    assert "recruit 3 more teammates" in exclude
    assert member_limits.requested_team_size_user_message(4, 3) == t(
        "enterprise.team.requested_size_over", language="en", requested=4, limit=3
    )
    assert "cannot add more teammates" in member_limits.teammate_quota_user_message(3)


@pytest.mark.asyncio
@pytest.mark.parametrize("leased", [False, True])
async def test_host_managed_registration_does_not_reserve_team_quota(monkeypatch, leased):
    svc = _QuotaSvc(fail=True, limit=1)
    monkeypatch.setattr("jiuwenswarm.enterprise.team.quota.get_team_quota_service", lambda: svc)
    agent, backend = _leader([("team-leader", "Leader", "ready")])
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1, team_id="tid" if leased else "")
    registered = await backend.spawn_member("colleague", "Colleague", object(), role=TeamRole.HOST_MANAGED)
    assert registered.ok
    assert backend.calls == ["colleague"]
    assert svc.reserved == [] and svc.committed == []
    blocked = await backend.spawn_member("expert", "Expert", object())
    assert not blocked.ok


@pytest.mark.asyncio
async def test_existing_host_managed_members_do_not_reduce_expert_capacity():
    rows = [("team-leader", "Leader", "ready"), ("colleague", "Colleague", "ready")]
    agent, backend = _leader(rows)
    backend.db.member.roles["colleague"] = "host_managed"
    member_limits.attach_teammate_limit_guard(agent, max_teammates=2)
    assert await member_limits.quota_member_names(backend) == ["team-leader"]
    assert await member_limits.count_team_members(backend) == 1
    assert await member_limits.count_non_leader_members(backend) == 0
    assert (await backend.spawn_member("expert", "Expert", object())).ok
    rows.append(("expert", "Expert", "ready"))
    assert not (await backend.spawn_member("second", "Second", object())).ok


@pytest.mark.asyncio
async def test_real_host_managed_api_registers_multiple_identities_at_capacity():
    from unittest.mock import AsyncMock
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
    from openjiuwen.agent_teams.tools.team import TeamBackend

    token = set_session_id("quota-host-members")
    database = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    try:
        await database.initialize()
        backend = TeamBackend("quota-team", "leader", True, database, AsyncMock())
        await backend.build_team(display_name="Group", desc="", leader_display_name="Leader", leader_desc="")
        agent = SimpleNamespace(role=TeamRole.LEADER, team_backend=backend)
        member_limits.attach_teammate_limit_guard(agent, max_teammates=1)
        for name in ("colleague-a", "colleague-b"):
            assert (await backend.spawn_host_managed_member(member_name=name, display_name=name)).ok
        assert await member_limits.quota_member_names(backend) == ["leader"]
        assert await member_limits.count_team_members(backend) == 1
        assert not (await backend.spawn_host_managed_member(member_name="colleague-a", display_name="Duplicate")).ok
        blocked = await backend.spawn_member("expert", "Expert", object())
        assert not blocked.ok
        assert blocked.reason.startswith(member_limits.TEAM_MEMBER_QUOTA_EXCEEDED)
    finally:
        await database.close()
        reset_session_id(token)


@pytest.mark.asyncio
async def test_public_group_roster_skips_guard_and_detaches_an_older_guard(monkeypatch):
    svc = _QuotaSvc(fail=True, limit=1)
    monkeypatch.setattr("jiuwenswarm.enterprise.team.quota.get_team_quota_service", lambda: svc)
    agent, backend = _leader([("team-leader", "Leader", "ready")])
    member_limits.attach_teammate_limit_guard(agent, max_teammates=1, team_id="team")
    assert not (await backend.spawn_member("blocked", "Blocked", object())).ok
    svc.reserved.clear()
    backend.conversation_mode = "group_chat"
    assert not member_limits.attach_teammate_limit_guard(agent, max_teammates=1, team_id="team")
    for name in ("expert", "expert-group", "colleague"):
        assert (await backend.spawn_member(name, name, object())).ok
    assert svc.reserved == []
    assert backend.calls == ["expert", "expert-group", "colleague"]


@pytest.mark.asyncio
async def test_real_group_roster_can_register_experts_above_team_limit():
    from unittest.mock import AsyncMock
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
    from openjiuwen.agent_teams.tools.team import TeamBackend
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    token = set_session_id("public-group-quota")
    database = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    try:
        await database.initialize()
        backend = TeamBackend("public-group", "leader", True, database, AsyncMock())
        backend.conversation_mode = "group_chat"
        await backend.build_team(display_name="Group", desc="", leader_display_name="Leader", leader_desc="")
        agent = SimpleNamespace(role=TeamRole.LEADER, team_backend=backend)
        assert not member_limits.attach_teammate_limit_guard(agent, max_teammates=5)
        for index in range(8):
            name = f"expert-{index}"
            assert (await backend.spawn_member(name, name, AgentCard(id=name, name=name))).ok
        assert (await backend.spawn_host_managed_member(member_name="colleague", display_name="Colleague")).ok
        assert len(await backend.db.member.get_member_roster(backend.team_name)) == 10
    finally:
        await database.close()
        reset_session_id(token)
