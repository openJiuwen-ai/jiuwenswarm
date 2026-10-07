# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for expert org adapter installer and shared-db launch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.agents.harness.team.expert_org.wiring import install_expert_org_adapters


class _OrgRuntime:
    def __init__(self, team_runtime_manager=None) -> None:
        self.catalog = None
        self.launcher = None
        self._team_runtime_manager = team_runtime_manager

    def set_expert_group_catalog(self, catalog) -> None:
        self.catalog = catalog

    def set_expert_team_launcher(self, launcher) -> None:
        self.launcher = launcher


def test_install_expert_org_adapters_injects_catalog_and_launcher() -> None:
    team_runtime = object()
    org = _OrgRuntime(team_runtime_manager=team_runtime)
    install_expert_org_adapters(org)
    assert org.catalog is not None
    assert org.launcher is not None
    assert org.launcher._runtime_manager is team_runtime

    catalog_id = id(org.catalog)
    launcher_id = id(org.launcher)
    install_expert_org_adapters(org)
    assert id(org.catalog) == catalog_id
    assert id(org.launcher) == launcher_id


def test_install_expert_org_adapters_noop_without_setters() -> None:
    install_expert_org_adapters(SimpleNamespace())  # should not raise


def test_register_expert_adapter_installer_sets_lazy_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Org:
        def set_expert_adapter_installer(self, installer) -> None:
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

    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        install_expert_org_adapters,
        register_expert_adapter_installer,
    )

    register_expert_adapter_installer()
    assert captured["installer"] is install_expert_org_adapters
    assert getattr(runner, "_team_runtime_manager") is not None


@pytest.mark.asyncio
async def test_launcher_shares_db_from_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = object()
    owner_backend = SimpleNamespace(db=shared_db)
    expert_backend = SimpleNamespace(
        db=shared_db,
        task_manager=SimpleNamespace(db=shared_db),
        message_manager=SimpleNamespace(db=shared_db),
        build_team_calls=[],
    )

    async def _build_team(**kwargs):
        expert_backend.build_team_calls.append(kwargs)

    expert_backend.build_team = _build_team

    class _Pool:
        async def get(self, team_name: str):
            if team_name == "owner-team":
                return SimpleNamespace(
                    team_name="owner-team",
                    agent=SimpleNamespace(team_backend=owner_backend),
                )
            return None

        async def teams_for_session(self, session_id: str):
            return []

    class _Runtime:
        def __init__(self) -> None:
            self.pool = _Pool()
            self.paused = False

        async def activate(self, spec, session_id):
            agent = SimpleNamespace(
                team_backend=expert_backend,
                member_name="leader-expert",
                spec=SimpleNamespace(
                    metadata={"capabilities": ["frontend"]},
                    leader=SimpleNamespace(display_name="Leader", desc="leader desc"),
                ),
            )
            return SimpleNamespace(agent=agent)

        async def pause(self, *, team_name, session_id):
            self.paused = True
            return True

        async def stop_team(self, *, team_name, session_id):
            return True

    runtime = _Runtime()
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    package_dir = object()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_dir",
        lambda _name: package_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.agent_group.load_agent_group_package_bundle",
        lambda _path: SimpleNamespace(
            instruction="group instruction",
            capabilities=("frontend",),
            templates={},
        ),
    )

    async def _fake_build(**kwargs):
        return SimpleNamespace(
            team_name=kwargs["team_id"],
            leader=SimpleNamespace(display_name="Leader", desc="leader desc"),
        )

    monkeypatch.setattr(launcher, "_build_enriched_spec", _fake_build)

    launched = await launcher.launch(
        organization_id="org-1",
        agent_group_name="sample-expert-group",
        session_id="sess-1",
        share_db_from_team_id="owner-team",
    )
    assert launched.team_id.startswith("org-expert-")
    assert len(launched.team_id.removeprefix("org-expert-")) == 12
    assert expert_backend.db is shared_db
    assert expert_backend.task_manager.db is shared_db
    assert expert_backend.message_manager.db is shared_db
    assert expert_backend.build_team_calls
    assert runtime.paused is True
