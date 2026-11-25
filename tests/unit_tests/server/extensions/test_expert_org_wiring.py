# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for expert org adapter installer and shared-db launch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.agents.harness.team.expert_org.wiring import install_expert_org_adapters


class _OrgRuntime:
    def __init__(self, team_runtime_manager=None) -> None:
        self.launcher = None
        self._team_runtime_manager = team_runtime_manager

    def set_expert_team_launcher(self, launcher) -> None:
        self.launcher = launcher


def test_install_expert_org_adapters_injects_launcher_without_a_group_catalog() -> None:
    team_runtime = object()
    org = _OrgRuntime(team_runtime_manager=team_runtime)
    install_expert_org_adapters(org)
    assert org.launcher is not None
    assert org.launcher._runtime_manager is team_runtime

    launcher_id = id(org.launcher)
    install_expert_org_adapters(org)
    assert id(org.launcher) == launcher_id


def test_install_expert_org_adapters_noop_without_setters() -> None:
    install_expert_org_adapters(SimpleNamespace())  # should not raise


@pytest.mark.asyncio
async def test_organization_invites_each_expert_group_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.organization import runtime
    from openjiuwen.agent_teams.organization.tools import OrgCreateAndInviteExpertTeamTool, OrgInviteTeamTool
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    from jiuwenswarm.agents.harness.team.expert_org.wiring import register_expert_adapter_installer

    manager = TeamRuntimeManager()
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", manager, raising=False)
    org_runtime = manager.organization_runtime_manager
    register_expert_adapter_installer()
    assert manager.organization_runtime_manager is org_runtime
    patched_invite = org_runtime.invite_team
    register_expert_adapter_installer()
    assert org_runtime.invite_team is patched_invite
    db = object()

    def entry(group: str) -> SimpleNamespace:
        return SimpleNamespace(
            current_session_id="session",
            agent=SimpleNamespace(
                spec=SimpleNamespace(agent_group_name=group, metadata={}),
                team_backend=SimpleNamespace(db=db, messager=None, is_leader=True, leader_member_name="leader"),
            ),
        )

    entries = {"owner": entry("business-planning"), "another-tech": entry("technology-insight")}
    monkeypatch.setattr(manager.pool, "get", AsyncMock(side_effect=entries.get))
    roster = [SimpleNamespace(team_id="owner")]
    organization = SimpleNamespace(organization_id="org", owner_team_id="owner", leaders=roster, model_dump=lambda: {})

    async def publish(event, **kwargs) -> None:
        if hasattr(event, "joined_team_id"):
            roster.append(SimpleNamespace(team_id=event.joined_team_id))

    mock_org = SimpleNamespace(get_organization=AsyncMock(return_value=organization), publish_event=publish)
    monkeypatch.setattr(runtime, "get_process_org_manager", lambda **kwargs: mock_org)
    async def bind(*, backend, **kwargs):
        backend.org_task_manager = SimpleNamespace(organization_id="org")

    monkeypatch.setattr(org_runtime, "_bind_team", AsyncMock(side_effect=bind))
    monkeypatch.setattr(org_runtime, "_resume_assignable_tasks", AsyncMock())

    async def launch(**kwargs) -> SimpleNamespace:
        await asyncio.sleep(0)
        entries["tech"] = entry(kwargs["agent_group_name"])
        return SimpleNamespace(
            team_id="tech", agent_group_name=kwargs["agent_group_name"], to_dict=lambda: {"team_id": "tech"},
        )

    launcher = SimpleNamespace(launch=AsyncMock(side_effect=launch))
    org_runtime.set_expert_adapter_installer(None)
    org_runtime.set_expert_team_launcher(launcher)
    create = OrgCreateAndInviteExpertTeamTool(org_runtime, "owner", "session")
    invite = OrgInviteTeamTool(org_runtime, "owner", "session")

    own_group = await create.invoke({"organization_id": "org", "agent_group_name": "business-planning"})
    assert not own_group.success and "team 'owner'" in own_group.error
    tech = {"organization_id": "org", "agent_group_name": "technology-insight"}
    first, second = await asyncio.gather(create.invoke(tech), create.invoke(tech))
    assert first.success and first.data["team_id"] == "tech"
    assert not second.success and "team 'tech'" in second.error
    launcher.launch.assert_awaited_once()

    duplicate = await invite.invoke({"organization_id": "org", "team_id": "another-tech"})
    assert not duplicate.success and "team 'tech'" in duplicate.error
    retry = await invite.invoke({"organization_id": "org", "team_id": "tech"})
    assert retry.success
    org_runtime._bind_team.assert_awaited_once()

    cold_entry = entries.pop("tech")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.expert_org.organization_runtime.load_organization_team_identity",
        lambda team_id: {"agent_group_name": "technology-insight"} if team_id == "tech" else {},
    )

    async def restore(**kwargs):
        entries["tech"] = cold_entry

    recover = AsyncMock(side_effect=restore)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "restore", recover)
    cold_retry = await invite.invoke({"organization_id": "org", "team_id": "tech"})
    assert cold_retry.success
    recover.assert_awaited_once_with(team_id="tech", session_id="session", organization=organization, db=db)
    entries.pop("tech")
    recover.side_effect = ValueError("package unavailable")
    failed_retry = await invite.invoke({"organization_id": "org", "team_id": "tech"})
    assert not failed_retry.success and failed_retry.error == "package unavailable"
    assert [leader.team_id for leader in roster] == ["owner", "tech"]

    # Keep the winning invite between its membership check and its join.
    entries["risk"] = entry("risk-analysis")
    invite_binding = asyncio.Event()
    release_invite = asyncio.Event()

    async def bind_risk(*, backend, **kwargs):
        invite_binding.set()
        await release_invite.wait()
        await bind(backend=backend, **kwargs)

    org_runtime._bind_team.side_effect = bind_risk
    pending_invite = asyncio.create_task(invite.invoke({"organization_id": "org", "team_id": "risk"}))
    await asyncio.wait_for(invite_binding.wait(), timeout=5)
    create_started = asyncio.Event()

    async def create_risk():
        create_started.set()
        return await create.invoke({"organization_id": "org", "agent_group_name": "risk-analysis"})

    pending_create = asyncio.create_task(create_risk())
    try:
        await asyncio.wait_for(create_started.wait(), timeout=5)
        launcher.launch.assert_awaited_once()
        assert not pending_create.done()
    finally:
        release_invite.set()
    invited, rejected = await asyncio.wait_for(asyncio.gather(pending_invite, pending_create), timeout=5)
    assert invited.success
    assert not rejected.success and "team 'risk'" in rejected.error
    launcher.launch.assert_awaited_once()
    assert [leader.team_id for leader in roster] == ["owner", "tech", "risk"]


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

    runtime = register_expert_adapter_installer()
    assert captured["installer"] is install_expert_org_adapters
    assert runtime is getattr(runner, "_team_runtime_manager")
    assert register_expert_adapter_installer() is runtime


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
        "openjiuwen.agent_teams.experts.catalog._process_catalog",
        SimpleNamespace(locate_group=AsyncMock(return_value=SimpleNamespace(path=package_dir))),
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.experts.load_agent_group_package_bundle",
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
