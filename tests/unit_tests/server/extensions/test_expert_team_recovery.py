# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recover invited Teams under their durable identity without creating a new Team."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.server.runtime import team_organization_history as history
from jiuwenswarm.server.runtime import team_organization_identity_store as identities


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": True}})
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {"agent_group_name": "technology-insight"})
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_dir", lambda name: name)
    monkeypatch.setattr("jiuwenswarm.agents.swarm.agent_group.load_agent_group_package_bundle", lambda path: object())
    organization = SimpleNamespace(
        organization_id="org", owner_team_id="owner",
        leaders=[SimpleNamespace(team_id=name) for name in ("owner", "expert")],
    )
    db = SimpleNamespace(team=SimpleNamespace(get_team=AsyncMock(return_value=SimpleNamespace(display_name="技术洞察专家团"))))
    entries = {}

    async def get(name):
        return entries.get(name)

    async def activate(spec, session_id):
        backend = SimpleNamespace(db=db, org_task_manager=SimpleNamespace(organization_id="org"))
        agent = SimpleNamespace(team_backend=backend)
        entries[spec.team_name] = SimpleNamespace(agent=agent, current_session_id=session_id)
        await asyncio.sleep(0)
        return SimpleNamespace(agent=agent)

    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=get), activate=AsyncMock(side_effect=activate),
        pause=AsyncMock(return_value=True), stop_team=AsyncMock(),
    )
    spec = SimpleNamespace(team_name="expert")
    build = AsyncMock(return_value=spec)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "_build_enriched_spec", build)
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    kwargs = dict(team_id="expert", session_id="session", organization=organization, db=db, channel_id="web")
    return launcher, runtime, kwargs, entries, build


@pytest.mark.asyncio
async def test_recovery_reuses_team_id_and_shared_database(recovery):
    launcher, runtime, kwargs, entries, build = recovery
    backend = await launcher.restore(**kwargs)
    assert backend.db is kwargs["db"]
    assert backend.org_task_manager.organization_id == "org"
    assert set(entries) == {"expert"}
    assert build.await_args.kwargs["team_id"] == "expert"
    assert build.await_args.kwargs["shared_db"] is kwargs["db"]
    runtime.activate.assert_awaited_once()
    runtime.pause.assert_awaited_once_with(team_name="expert", session_id="session")
    runtime.stop_team.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_launchers_recover_only_once(recovery):
    launcher, runtime, kwargs, _, _ = recovery
    second = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    first_backend, second_backend = await asyncio.gather(launcher.restore(**kwargs), second.restore(**kwargs))
    assert first_backend is second_backend
    runtime.activate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["owner", "foreign"])
async def test_recovery_rejects_owner_or_foreign_team(recovery, target):
    launcher, runtime, kwargs, _, _ = recovery
    kwargs["team_id"] = target
    with pytest.raises(ValueError, match="not an invited expert"):
        await launcher.restore(**kwargs)
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_rejects_different_session(recovery):
    launcher, runtime, kwargs, entries, _ = recovery
    entries["expert"] = SimpleNamespace(current_session_id="other-session")
    with pytest.raises(ValueError, match="another session"):
        await launcher.restore(**kwargs)
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_is_disabled_with_feature_switch(recovery, monkeypatch):
    launcher, runtime, kwargs, _, _ = recovery
    monkeypatch.setattr(history, "get_config", lambda: {})
    with pytest.raises(ValueError, match="disabled"):
        await launcher.restore(**kwargs)
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [0, 1, 2])
async def test_legacy_recovery_requires_unique_installed_package(recovery, monkeypatch, matches):
    launcher, runtime, kwargs, _, build = recovery
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {})
    group = SimpleNamespace(agent_group_name="technology-insight", display_name="技术洞察专家团")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.expert_org.catalog.JiuwenExpertGroupCatalog.list", lambda: [group] * matches
    )
    if matches == 1:
        await launcher.restore(**kwargs)
        assert build.await_args.kwargs["agent_group_name"] == "technology-insight"
    else:
        with pytest.raises(ValueError, match="missing or ambiguous"):
            await launcher.restore(**kwargs)
        runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_package_does_not_activate(recovery, monkeypatch):
    launcher, runtime, kwargs, _, _ = recovery

    def missing(name):
        raise ValueError("package is not installed")

    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_dir", missing)
    with pytest.raises(ValueError, match="not installed"):
        await launcher.restore(**kwargs)
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_recovery_rolls_back_new_runtime(recovery):
    launcher, runtime, kwargs, _, _ = recovery
    runtime.pause.return_value = False
    with pytest.raises(ValueError, match="paused state"):
        await launcher.restore(**kwargs)
    runtime.stop_team.assert_awaited_once_with(team_name="expert", session_id="session")
