# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for summary org factory installer and recover team-id reuse."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.summary_team_org.factory import (
    JiuwenSummaryTeamFactory,
    _leader_id_from_agent,
    _summary_team_id_for_task,
)
from jiuwenswarm.agents.harness.team.summary_team_org.wiring import (
    install_summary_factory,
)


class _OrgRuntime:
    def __init__(self, team_runtime_manager=None) -> None:
        self.factory = None
        self._team_runtime_manager = team_runtime_manager

    def set_summary_team_factory(self, factory) -> None:
        self.factory = factory


def test_install_summary_factory_injects_factory() -> None:
    team_runtime = object()
    org = _OrgRuntime(team_runtime_manager=team_runtime)
    install_summary_factory(org)
    assert org.factory is not None
    assert org.factory._runtime_manager is team_runtime

    factory_id = id(org.factory)
    install_summary_factory(org)
    assert id(org.factory) == factory_id


def test_install_summary_factory_noop_without_setter() -> None:
    install_summary_factory(SimpleNamespace())  # should not raise


def test_register_summary_factory_installer_sets_lazy_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Org:
        def set_summary_team_factory_installer(self, installer) -> None:
            captured["installer"] = installer

    class _TeamRuntime:
        organization_runtime_manager = _Org()

    class _Runner:
        pass

    runner = _Runner()
    import sys

    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.agent_teams.runtime",
        SimpleNamespace(TeamRuntimeManager=lambda: _TeamRuntime()),
    )
    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.core.runner.runner",
        SimpleNamespace(GLOBAL_RUNNER=runner),
    )

    from jiuwenswarm.agents.harness.team.summary_team_org.wiring import (
        install_summary_factory,
        register_summary_factory_installer,
    )

    register_summary_factory_installer()
    assert captured["installer"] is install_summary_factory
    assert getattr(runner, "_team_runtime_manager") is not None


@pytest.mark.asyncio
async def test_leader_id_prefers_resolver() -> None:
    """resolver 有答案时优先采用它（可覆盖 member_name 的误导）。"""
    resolved_calls: list[bool] = []

    class _Backend:
        member_name = "member-x"
        leader_member_name = ""  # a plain member's backend: not the leader

        async def resolve_leader_member_name(self) -> str:
            resolved_calls.append(True)
            return "team-leader"

    agent = SimpleNamespace(team_backend=_Backend(), member_name="member-x")
    assert await _leader_id_from_agent(agent, "org-summary-abc") == "team-leader"
    assert resolved_calls == [True]


@pytest.mark.asyncio
async def test_leader_id_falls_back_to_team_id_when_resolver_empty() -> None:
    """resolver 返回空（或不存在）时用确定性兜底，绝不返回空串。"""

    class _EmptyResolverBackend:
        leader_member_name = "from-field"
        member_name = "member-x"

        async def resolve_leader_member_name(self) -> str:
            return ""

    # An empty resolver answer is not trusted, even though the fields carry a
    # name: the deterministic floor wins so register_leader never sees "".
    agent = SimpleNamespace(team_backend=_EmptyResolverBackend())
    assert await _leader_id_from_agent(agent, "t-1") == "leader-t-1"

    # A backend without the resolver at all lands on the same floor.
    legacy = SimpleNamespace(team_backend=SimpleNamespace(leader_member_name="legacy-leader"))
    assert await _leader_id_from_agent(legacy, "t-1") == "leader-t-1"


@pytest.mark.asyncio
async def test_leader_id_floor_without_backend() -> None:
    """没有 team_backend 时同样用 leader-<team_id> 兜底。"""
    assert await _leader_id_from_agent(SimpleNamespace(), "t-2") == "leader-t-2"
    assert await _leader_id_from_agent(SimpleNamespace(team_backend=SimpleNamespace()), "t-2") == "leader-t-2"


def test_summary_team_id_for_task_is_deterministic() -> None:
    a = _summary_team_id_for_task("summary-task-1")
    b = _summary_team_id_for_task("summary-task-1")
    c = _summary_team_id_for_task("summary-task-2")
    assert a == b
    assert a.startswith("org-summary-")
    assert len(a) == len("org-summary-") + 12
    assert a != c


@pytest.mark.asyncio
async def test_recover_converges_on_deterministic_team_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover launches on the deterministic id and is idempotent across retries."""
    task_id = "summary-task-1"
    expected = _summary_team_id_for_task(task_id)

    factory = JiuwenSummaryTeamFactory(runtime_manager=object())
    launched_calls: list[dict] = []

    async def _fake_launch(**kwargs) -> SimpleNamespace:
        launched_calls.append(kwargs)
        return SimpleNamespace(team_id=kwargs["team_id"], leader_id="leader-summary")

    monkeypatch.setattr(factory, "_launch_team", _fake_launch)

    first = await factory.recover(
        execution_id="summary-exec-1",
        organization_id="org-1",
        root_task_id="root-1",
        summary_task_id=task_id,
        session_id="sess-1",
    )
    # A second recovery of the same execution must converge on the same team
    # name, so repeated §8 scans never spawn duplicate teams.
    second = await factory.recover(
        execution_id="summary-exec-1",
        organization_id="org-1",
        root_task_id="root-1",
        summary_task_id=task_id,
        session_id="sess-1",
    )
    assert first.team_id == expected
    assert second.team_id == expected
    assert [c["team_id"] for c in launched_calls] == [expected, expected]
