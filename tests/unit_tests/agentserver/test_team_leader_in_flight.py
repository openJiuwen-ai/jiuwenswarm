"""leader 成员行缺失时的 settle 读数口径。

``team_info`` 行存在而 leader 成员行缺失，是确定性不在途（半建成 /
被移除），不是读数瞬态。把它当 None 会让 settle 复核永久未知、终态
永不发出，团队流只剩 keepalive。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_stall_watchdog


class _FakeMemberDao:
    def __init__(self, leader_row=None, error=None) -> None:
        self._row = leader_row
        self._error = error

    async def get_member(self, member_name, team_name=None):
        if self._error is not None:
            raise self._error
        return self._row


class _FakeTeamDao:
    def __init__(self, team) -> None:
        self._team = team

    async def get_team(self, team_name):
        return self._team


class _FakeDb:
    def __init__(self, team, member_dao) -> None:
        self.team = _FakeTeamDao(team)
        self.member = member_dao

    async def initialize(self) -> None:
        return None


def _patch_world(monkeypatch: pytest.MonkeyPatch, db) -> None:
    class _TeamManager:
        def get_active_team_name(self, session_id):
            return "t1"

    monkeypatch.setattr(
        team_stall_watchdog, "get_team_manager", lambda _cid: _TeamManager()
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid, **_kwargs: {"team_name": "t1"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.resolve_team_sqlite_db_path",
        lambda _cfg: "team.db",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.spawn.shared_resources.get_shared_db",
        lambda _cfg: db,
    )


@pytest.mark.asyncio
async def test_missing_leader_row_is_not_in_flight(monkeypatch) -> None:
    """团队行在、leader 行缺 → False（确定性不在途），不是未知 None。"""
    db = _FakeDb(
        SimpleNamespace(leader_member_name="team-leader"),
        _FakeMemberDao(leader_row=None),
    )
    _patch_world(monkeypatch, db)
    assert await team_stall_watchdog.team_leader_in_flight("desktop", "s1") is False


@pytest.mark.asyncio
async def test_leader_row_execution_status_drives_liveness(monkeypatch) -> None:
    """leader 行存在时仍按 execution_status 判定在途（既有语义不变）。"""
    db = _FakeDb(
        SimpleNamespace(leader_member_name="team-leader"),
        _FakeMemberDao(leader_row=SimpleNamespace(execution_status="running")),
    )
    _patch_world(monkeypatch, db)
    assert await team_stall_watchdog.team_leader_in_flight("desktop", "s1") is True


@pytest.mark.asyncio
async def test_leader_row_read_failure_stays_unknown(monkeypatch) -> None:
    """真正的读失败仍是未知 None（settle 续窗重试，不抢跑）。"""
    db = _FakeDb(
        SimpleNamespace(leader_member_name="team-leader"),
        _FakeMemberDao(error=RuntimeError("read pool timeout")),
    )
    _patch_world(monkeypatch, db)
    assert await team_stall_watchdog.team_leader_in_flight("desktop", "s1") is None
