# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recover invited Teams under their durable identity without creating a new Team."""

import asyncio
import json
from contextvars import Context, ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.server.runtime import team_organization_history as history
from jiuwenswarm.server.runtime import team_organization_identity_store as identities


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,target_field", [
    ("org_create_task", "delegated_to_team_id"),
    ("org_delegate_task", "to_team_id"),
    ("org_send_leader_message", "to_team_id"),
])
async def test_owner_dispatch_restores_cold_expert_before_tool(recovery, monkeypatch, tool_name, target_field):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    from jiuwenswarm.agents.harness.team.rails.organization_availability_rail import OrganizationAvailabilityRail

    launcher, runtime, kwargs, entries, _ = recovery
    runtime.organization_runtime_manager = SimpleNamespace(set_leader_turn_runner=Mock())
    owner_manager = SimpleNamespace(get_organization=AsyncMock(return_value=kwargs["organization"]))
    entries["owner"] = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(
        team_backend=SimpleNamespace(db=kwargs["db"], org_task_manager=owner_manager),
    ))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "_get_runtime", lambda self: runtime)
    rail = OrganizationAvailabilityRail("owner", "session")
    monkeypatch.setattr(rail, "_team_availability", AsyncMock(return_value={
        "available": True, "agent_group_name": "technology-insight",
    }))
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_name=tool_name, tool_args={target_field: "expert"}), extra={})
    await rail.before_tool_call(ctx)
    assert "expert" in entries
    runtime.activate.assert_awaited_once()
    assert entries["expert"].agent.team_backend.db is kwargs["db"]
    runtime.organization_runtime_manager.set_leader_turn_runner.assert_called_once()
    assert not ctx.extra.get("_skip_tool")
    # Subsequent work must reuse the same live instance.
    await rail.before_tool_call(ctx)
    runtime.activate.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_dispatch_recovery_failure_blocks_delegation(recovery, monkeypatch):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    from jiuwenswarm.agents.harness.team.rails.organization_availability_rail import OrganizationAvailabilityRail

    _, runtime, kwargs, entries, _ = recovery
    entries["owner"] = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(
        team_backend=SimpleNamespace(db=kwargs["db"], org_task_manager=SimpleNamespace(
            get_organization=AsyncMock(return_value=kwargs["organization"]),
        )),
    ))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    restore = AsyncMock(side_effect=ValueError("package resources are broken"))
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "restore", restore)
    rail = OrganizationAvailabilityRail("owner", "session")
    monkeypatch.setattr(rail, "_team_availability", AsyncMock(return_value={
        "available": True, "agent_group_name": "technology-insight",
    }))
    ctx = SimpleNamespace(inputs=ToolCallInputs(
        tool_name="org_create_task", tool_args={"delegated_to_team_id": "expert"},
    ), extra={})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "package resources are broken" in ctx.inputs.tool_result["error"]
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id,target,session_id", [
    ("someone-else", "expert", "session"),
    ("owner", "foreign", "session"),
    ("owner", "expert", "another-session"),
])
async def test_dispatch_does_not_restore_foreign_membership_or_session(
    recovery, monkeypatch, owner_id, target, session_id,
):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.agents.harness.team.rails.organization_availability_rail import OrganizationAvailabilityRail

    _, runtime, kwargs, entries, _ = recovery
    kwargs["organization"].owner_team_id = owner_id
    entries["owner"] = SimpleNamespace(current_session_id=session_id, agent=SimpleNamespace(
        team_backend=SimpleNamespace(db=kwargs["db"], org_task_manager=SimpleNamespace(
            get_organization=AsyncMock(return_value=kwargs["organization"]),
        )),
    ))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    restore = AsyncMock()
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "restore", restore)
    await OrganizationAvailabilityRail("owner", "session")._restore_dispatch_target(target)
    restore.assert_not_awaited()


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": True}})
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {"agent_group_name": "technology-insight"})
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_dir", lambda name: name)
    monkeypatch.setattr(
        "openjiuwen.agent_teams.experts.catalog._process_catalog",
        SimpleNamespace(locate_group=AsyncMock(side_effect=lambda name: SimpleNamespace(path=name))),
    )
    monkeypatch.setattr("openjiuwen.agent_teams.experts.load_agent_group_package_bundle", lambda path: object())
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
    from openjiuwen.agent_teams.experts import AgentGroupCard

    group = AgentGroupCard(
        name="technology-insight", description="Tech insights",
        display_name={"zh": "技术洞察专家团", "en": "Technology Insight"},
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.experts.catalog._process_catalog",
        SimpleNamespace(
            locate_group=AsyncMock(return_value=SimpleNamespace(path="technology-insight")),
            list_group_packages=AsyncMock(return_value=[SimpleNamespace(name=group.name, path=group.name)] * matches),
        ),
    )
    monkeypatch.setattr("openjiuwen.agent_teams.experts.read_agent_group_card", AsyncMock(return_value=group))
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

    async def missing(name):
        raise ValueError("package is not installed")

    monkeypatch.setattr(
        "openjiuwen.agent_teams.experts.catalog._process_catalog",
        SimpleNamespace(locate_group=missing),
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("group_display_name", ["", {"zh": "明确团名称"}])
async def test_legacy_leader_label_is_used_only_without_a_group_display_name(
    recovery, monkeypatch, group_display_name,
):
    from openjiuwen.agent_teams.experts import AgentGroupCard, AgentGroupMemberCard
    from openjiuwen.harness.resources.template_card import AgentTemplateCard

    launcher, _, _, _, _ = recovery
    card = AgentGroupCard(
        name="technology-insight", description="Group introduction", display_name=group_display_name,
        members=(AgentGroupMemberCard(
            name="leader", group="technology-insight",
            card=AgentTemplateCard(name="Legacy Lead", description="Leader introduction"),
        ),),
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.experts.catalog._process_catalog",
        SimpleNamespace(
            list_group_packages=AsyncMock(return_value=[SimpleNamespace(name=card.name, path=card.name)]),
        ),
    )
    monkeypatch.setattr("openjiuwen.agent_teams.experts.read_agent_group_card", AsyncMock(return_value=card))
    if not group_display_name:
        assert await launcher._legacy_group_name("Legacy Lead") == "technology-insight"
    else:
        with pytest.raises(ValueError, match="missing or ambiguous"):
            await launcher._legacy_group_name("Legacy Lead")


@pytest.mark.asyncio
async def test_no_registered_locator_does_not_fall_back_to_disk(recovery, monkeypatch):
    from jiuwenswarm.server.runtime.extension_package_manager import AgentGroupPackageError

    launcher, runtime, _, _, _ = recovery
    monkeypatch.setattr("openjiuwen.agent_teams.experts.catalog._process_catalog", None)
    with pytest.raises(AgentGroupPackageError, match="catalog is not configured") as error:
        await launcher._locate_group("technology-insight")
    assert error.value.code == "AGENT_GROUP_CATALOG_NOT_CONFIGURED"
    assert error.value.code in str(error.value)
    assert "configure_agent_teams_home()" in str(error.value)
    runtime.activate.assert_not_awaited()


@pytest.fixture
def enterprise_background_recovery(recovery, monkeypatch, tmp_path):
    from agent_gateway.enterprise.capabilities.experts import bound_packages
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.agents.harness.team.experts import catalog
    from jiuwenswarm.enterprise.capabilities.experts import request_packages as allowlist
    from jiuwenswarm.server.runtime.session import session_metadata

    launcher, runtime, kwargs, entries, _ = recovery
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    marketplace = tmp_path / "marketplace.json"
    builtin = tmp_path / "built_in"
    (builtin / "technology-insight").mkdir(parents=True)
    installed = {"id": "technology-insight", "installed": True, "source": "hub"}
    marketplace.write_text(json.dumps({"plugins": [installed]}), encoding="utf-8")
    monkeypatch.setattr(catalog, "expert_package_roots", lambda _: (marketplace, tmp_path / "local", builtin))
    monkeypatch.setattr("openjiuwen.agent_teams.experts.catalog._process_catalog", catalog.JiuwenExpertCatalog())
    monkeypatch.setattr(allowlist, "_REQUEST_PACKAGES", ContextVar("test-background-packages", default=None))
    monkeypatch.setattr(allowlist, "_hub_record", lambda *_: SimpleNamespace(
        asset_id="insight-asset", package_id="technology-insight",
    ))
    grants = [{"kind": "agent_group", "id": "insight-asset"}]
    read_grants = AsyncMock(return_value=grants)
    monkeypatch.setattr(bound_packages, "packages_for_bot", read_grants)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: {
        "channel_metadata": {"user_id": "alice", "routing": {"group_id": "group", "bot_id": "bot-alice"}, allowlist.METADATA_KEY: grants},
    })
    runtime.organization_runtime_manager = SimpleNamespace(set_leader_turn_runner=Mock())
    entries["owner"] = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(
        team_backend=SimpleNamespace(db=kwargs["db"], org_task_manager=SimpleNamespace(
            get_organization=AsyncMock(return_value=kwargs["organization"]),
        )),
    ))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "_get_runtime", lambda self: runtime)
    return SimpleNamespace(
        launcher=launcher, runtime=runtime, entries=entries, read_grants=read_grants,
        marketplace=marketplace, installed=installed, grants=grants,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["org_root_delivery", "org_root_background"])
@pytest.mark.parametrize("state", ["allowed", "revoked", "uninstalled", "read_error"])
async def test_enterprise_background_dispatch_uses_current_grants_and_real_catalog(
    enterprise_background_recovery, monkeypatch, source, state,
):
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    from jiuwenswarm.agents.harness.team.rails.organization_availability_rail import OrganizationAvailabilityRail
    from jiuwenswarm.enterprise.capabilities.experts import request_packages as allowlist

    fixture = enterprise_background_recovery
    if state == "revoked":
        fixture.read_grants.return_value = []
    elif state == "read_error":
        fixture.read_grants.side_effect = RuntimeError("database unavailable")
    elif state == "uninstalled":
        fixture.installed["installed"] = False
        fixture.marketplace.write_text(json.dumps({"plugins": [fixture.installed]}), encoding="utf-8")
    rail = OrganizationAvailabilityRail("owner", "session")
    monkeypatch.setattr(rail, "_team_availability", AsyncMock(return_value={
        "available": True, "agent_group_name": "technology-insight",
    }))
    ctx = SimpleNamespace(inputs=ToolCallInputs(
        tool_name="org_delegate_task", tool_args={"to_team_id": "expert"},
    ), extra={})

    async def run_turn(*args, **kwargs):
        await rail.before_tool_call(ctx)
        return not ctx.extra.get("_skip_tool", False)

    monkeypatch.setattr(fixture.launcher, "_run_organization_turn", run_turn)
    result = await asyncio.create_task(fixture.launcher.run_organization_turn(
        "owner", "session", {"query": "Continue"}, source=source,
    ), context=Context())

    assert result is (state == "allowed")
    fixture.read_grants.assert_awaited_once_with("bot-alice")
    assert allowlist._REQUEST_PACKAGES.get() is None
    if state == "allowed":
        fixture.runtime.activate.assert_awaited_once()
        assert "expert" in fixture.entries
    else:
        fixture.runtime.activate.assert_not_awaited()
        assert "could not be restored" in ctx.inputs.tool_result["error"]

