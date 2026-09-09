# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for summary org factory installer and recover team-id reuse."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.summary_team_org.factory import (
    JiuwenSummaryTeamFactory,
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


def test_summary_team_id_for_task_is_deterministic() -> None:
    a = _summary_team_id_for_task("summary-task-1")
    b = _summary_team_id_for_task("summary-task-1")
    c = _summary_team_id_for_task("summary-task-2")
    assert a == b
    assert a.startswith("org-summary-")
    assert len(a) == len("org-summary-") + 12
    assert a != c


@pytest.mark.asyncio
async def test_recover_reuses_bound_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recover adopts the shared-DB bound id when a leader record exists."""
    task_id = "summary-task-1"
    bound_id = _summary_team_id_for_task(task_id)

    factory = JiuwenSummaryTeamFactory(runtime_manager=object())
    launched_calls: list[dict] = []

    async def _fake_find_bound(team_id: str, session_id: str) -> str:
        return team_id  # shared DB has a leader record -> reuse

    async def _fake_launch(**kwargs) -> SimpleNamespace:
        launched_calls.append(kwargs)
        return SimpleNamespace(team_id=kwargs["team_id"], leader_id="leader-summary")

    monkeypatch.setattr(factory, "_find_bound_team_id", _fake_find_bound)
    monkeypatch.setattr(factory, "_launch_team", _fake_launch)

    launched = await factory.recover(
        execution_id="summary-exec-1",
        organization_id="org-1",
        root_task_id="root-1",
        summary_task_id=task_id,
        session_id="sess-1",
    )
    assert launched.team_id == bound_id
    assert launched_calls[0]["team_id"] == bound_id


@pytest.mark.asyncio
async def test_recover_cold_starts_on_deterministic_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover falls back to the deterministic id when no leader record exists."""
    task_id = "summary-task-2"
    bound_id = _summary_team_id_for_task(task_id)

    factory = JiuwenSummaryTeamFactory(runtime_manager=object())
    launched_calls: list[dict] = []

    async def _fake_find_bound(team_id: str, session_id: str) -> str:
        return ""  # no leader record -> cold start

    async def _fake_launch(**kwargs) -> SimpleNamespace:
        launched_calls.append(kwargs)
        return SimpleNamespace(team_id=kwargs["team_id"], leader_id="leader-summary")

    monkeypatch.setattr(factory, "_find_bound_team_id", _fake_find_bound)
    monkeypatch.setattr(factory, "_launch_team", _fake_launch)

    launched = await factory.recover(
        execution_id="summary-exec-2",
        organization_id="org-2",
        root_task_id="root-2",
        summary_task_id=task_id,
        session_id="sess-2",
    )
    assert launched.team_id == bound_id
    assert launched_calls[0]["team_id"] == bound_id
