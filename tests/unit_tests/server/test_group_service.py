"""Group API semantics with real package compilation and controlled runtimes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec, RailSpec
from openjiuwen.agent_teams.schema.team import MemberOpResult
from openjiuwen.agent_teams.schema.status import MemberMode
from openjiuwen.agent_teams.paths import (
    set_task_openjiuwen_home,
    reset_task_openjiuwen_home,
)
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from jiuwenswarm.agents.swarm.group_chat import (
    assemble_group_members,
    GROUP_MEMBER_TEMPLATE,
    GroupMemberTemplateRail,
)
from jiuwenswarm.runtime.session_provisioner import SessionProvisionState
from jiuwenswarm.server.runtime import group_service as gs
from tests.agents.swarm.test_agent_group import _minimal_group


@pytest.fixture
def service(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(gs, "update_session_metadata", Mock())
    monkeypatch.setattr(gs.lifecycle, "guard", Mock())
    monkeypatch.setattr(
        packages, "resolve_agent_group_dir", lambda _: _minimal_group(tmp_path)
    )
    monkeypatch.setattr(
        packages,
        "resolve_agent_template_dir",
        lambda _: _minimal_group(tmp_path) / "agents" / "leader",
    )
    home = set_task_openjiuwen_home(tmp_path / "core")
    locks = {}

    async def compile_spec(session_id, *, request_metadata, **kwargs):
        spec = TeamAgentSpec(
            agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
            team_name=f"team-{session_id}",
            leader=LeaderSpec(member_name="host"),
        )
        assemble_group_members(spec, request_metadata["group_members"])
        spec.agents["teammate"].rails = [
            RailSpec(type=GROUP_MEMBER_TEMPLATE, params={"initial_names": []})
        ]
        return spec

    manager = SimpleNamespace(
        get_swarm_enriched_team_spec=AsyncMock(side_effect=compile_spec),
        get_startup_lock=lambda sid: locks.setdefault(sid, asyncio.Lock()),
        stop_session_runtime=AsyncMock(),
    )

    async def prepare(input):
        return SimpleNamespace(
            state=SessionProvisionState.PREPARED,
            result=SimpleNamespace(
                session_id=f"group-{input.create_token}",
                canonical_mode="team",
                project_dir="",
            ),
        )

    runtime = SimpleNamespace(
        start=AsyncMock(),
        prepare_session_create=AsyncMock(side_effect=prepare),
        abort_session_provision=AsyncMock(),
        commit_session_provision=AsyncMock(),
        delete_session=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    resolver = AsyncMock(return_value=None)
    result = gs.GroupService(
        runtime, manager_factory=lambda _: manager, backend_resolver=resolver
    )
    result.test_manager = manager
    try:
        yield result
    finally:
        reset_task_openjiuwen_home(home)


async def create(service, token="one", **params):
    info, _, _ = await service.prepare_create(
        {"name": "Research", "create_token": token, **params},
        user_id="owner",
        channel_id="web",
        request_id="request",
    )
    return info


@pytest.mark.asyncio
async def test_create_list_update_and_owner_isolation(service):
    group = await create(service)
    assert not {"compiled", "user_id", "receipts"} & group.keys()
    assert group["members"] == []
    assert service.list({}, user_id="owner", channel_id="web")["total"] == 1
    assert service.list({}, user_id="other", channel_id="web")["total"] == 0
    for user, channel in [("other", "web"), ("owner", "tui")]:
        with pytest.raises(gs.GroupError, match="not found"):
            service.get(group["group_id"], user_id=user, channel_id=channel)
    changed = await service.update(
        group["group_id"],
        {"name": "Updated", "description": "New"},
        user_id="owner",
        channel_id="web",
    )
    assert changed["name"] == "Updated"
    assert changed["description"] == "New"
    with pytest.raises(gs.GroupError, match="only accepts"):
        await service.update(
            group["group_id"], {"members": []}, user_id="owner", channel_id="web"
        )


@pytest.mark.asyncio
async def test_create_retry_conflict_and_invalid_definition(service):
    first = await create(service)
    assert await create(service) == first
    with pytest.raises(gs.GroupError, match="different group"):
        await create(service, name="Other")
    with pytest.raises(ValueError):
        await create(
            service, token="bad", members=[{"kind": "bad", "member_name": "one"}]
        )
    service.runtime.abort_session_provision.assert_awaited()
    assert gs.read_group("group-bad") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent_template", "agent_group"])
async def test_add_offline_and_restore_compiled_capabilities(service, kind):
    group = await create(service)
    member = dict(kind=kind, id="group", member_name="research")
    args = dict(user_id="owner", channel_id="web", request_id="add-1")
    result = await service.add_member(group["group_id"], {"member": member}, **args)
    assert result["event_type"] == "group.member.added"
    assert (
        await service.add_member(group["group_id"], {"member": member}, **args)
        == result
    )
    state = gs.read_group(group["group_id"])
    assert state["members"] == [member]
    compiled = state["compiled"]["research"]
    assert "Leader rules" in compiled["member"]["prompt"]
    assert bool(
        [
            t
            for t in compiled["template"]["tools"]
            if t["type"] == "swarm.group_run_team"
        ]
    ) == (kind == "agent_group")
    rail = GroupMemberTemplateRail(
        SimpleNamespace(session_id=group["group_id"], member_name="research"), set()
    )
    agent = SimpleNamespace(load_agent_template_spec=AsyncMock())
    await rail.before_invoke(SimpleNamespace(agent=agent))
    await rail.before_invoke(SimpleNamespace(agent=agent))
    agent.load_agent_template_spec.assert_awaited_once()


def live_backend():
    registered = set()

    async def register(**params):
        registered.add(params["member_name"])
        return MemberOpResult.success()

    async def get(name, team):
        return object() if name in registered else None

    return SimpleNamespace(
        conversation_mode="group_chat",
        team_name="team-group-one",
        leader_member_name="host",
        allocate_model=Mock(return_value=None),
        teammate_mode=MemberMode.BUILD_MODE,
        db=SimpleNamespace(
            initialize=AsyncMock(),
            team=SimpleNamespace(get_team=AsyncMock(return_value=object())),
            member=SimpleNamespace(get_member=AsyncMock(side_effect=get)),
        ),
        spawn_member=AsyncMock(side_effect=register),
        message_manager=SimpleNamespace(send_message=AsyncMock()),
        group_chat_spec=SimpleNamespace(
            agents={
                "teammate": SimpleNamespace(
                    rails=[
                        RailSpec(
                            type=GROUP_MEMBER_TEMPLATE, params={"initial_names": []}
                        )
                    ]
                )
            }
        ),
    )


@pytest.mark.asyncio
async def test_live_add_registers_and_notifies_once_without_mutating_spec(service):
    group = await create(service)
    backend = live_backend()
    service.backend_resolver.return_value = backend
    member = dict(kind="agent_group", id="group", member_name="research")
    original = backend.group_chat_spec
    for _ in range(2):
        await service.add_member(
            group["group_id"],
            {"member": member},
            user_id="owner",
            channel_id="web",
            request_id="stable",
        )
    backend.spawn_member.assert_awaited_once()
    backend.message_manager.send_message.assert_awaited_once()
    assert backend.group_chat_spec is original
    service.test_manager.stop_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_template_binds_real_run_team_tool_before_first_model_call(service):
    from openjiuwen.harness import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.agent_teams.rails.team_context import inject_team_handles
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    group = await create(service)
    backend = live_backend()
    service.backend_resolver.return_value = backend
    await service.add_member(
        group["group_id"],
        {
            "member": dict(
                kind="agent_group", id="group", member_name="research"
            )
        },
        user_id="owner",
        channel_id="web",
        request_id="one",
    )
    register_swarm_providers()
    context = SwarmBuildContext(session_id=group["group_id"], member_name="research")
    inject_team_handles(context.extras, team_backend=backend)
    agent = DeepAgent(AgentCard(id="group-proxy-binding-test", name="Proxy")).configure(
        DeepAgentConfig()
    )
    rail = GroupMemberTemplateRail(context, set())
    await rail.before_invoke(SimpleNamespace(agent=agent))
    assert "run_team" in [tool.name for tool in agent.deep_config.tools]


@pytest.mark.asyncio
async def test_failed_live_registration_can_be_repaired_with_same_request(service):
    group = await create(service)
    backend = live_backend()
    backend.spawn_member.side_effect = [
        MemberOpResult.fail("transient"),
        MemberOpResult.success(),
    ]
    service.backend_resolver.return_value = backend
    params = {
        "member": dict(kind="agent_template", id="one", member_name="research")
    }
    with pytest.raises(gs.GroupError, match="transient"):
        await service.add_member(
            group["group_id"],
            params,
            user_id="owner",
            channel_id="web",
            request_id="stable",
        )
    assert (
        await service.add_member(
            group["group_id"],
            params,
            user_id="owner",
            channel_id="web",
            request_id="stable",
        )
    )["status"] == "registered"
    assert len(gs.read_group(group["group_id"])["members"]) == 1


@pytest.mark.asyncio
async def test_parallel_additions_do_not_lose_roster_entries(service):
    group = await create(service)

    async def add(name):
        return await service.add_member(
            group["group_id"],
            {"member": dict(kind="agent_template", id="one", member_name=name)},
            user_id="owner",
            channel_id="web",
            request_id=name,
        )

    await asyncio.gather(add("first"), add("second"))
    assert {m["member_name"] for m in gs.read_group(group["group_id"])["members"]} == {
        "first",
        "second",
    }


@pytest.mark.asyncio
async def test_cli_expert_keeps_runtime_config_and_rejects_unsafe_live_upgrade(
    service, tmp_path, monkeypatch
):
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    package = _minimal_group(tmp_path)
    manifest_path = package / "agents" / "member1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runtime"] = {
        "provider_name": "codex",
        "provider_version": "0.1.0",
        "config": {"skill_conflict": "replace"},
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        packages, "resolve_agent_template_dir", lambda _: manifest_path.parent
    )
    group = await create(service)
    member = dict(kind="agent_template", id="cli-expert", member_name="coding")
    backend = live_backend()
    backend.external_cli_config = Mock(return_value=None)
    service.backend_resolver.return_value = backend
    with pytest.raises(gs.GroupError, match="CLI configuration"):
        await service.add_member(
            group["group_id"],
            {"member": member},
            user_id="owner",
            channel_id="web",
            request_id="cli",
        )
    assert gs.read_group(group["group_id"])["members"] == []
    service.backend_resolver.return_value = None
    await service.add_member(
        group["group_id"],
        {"member": member},
        user_id="owner",
        channel_id="web",
        request_id="cli",
    )
    raw = gs.read_group(group["group_id"])["compiled"]["coding"]["member"]
    assert raw["role_type"] == "external_cli"
    assert raw["external_cli"]["skill_conflict"] == "replace"


@pytest.mark.asyncio
async def test_added_proxy_can_be_mentioned_in_real_group_database(service):
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
    from openjiuwen.agent_teams.tools.team import TeamBackend
    from openjiuwen.agent_teams.group_chat.handler import deliver_group_message
    from openjiuwen.agent_teams.interaction.payload import GroupChatMessage
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id

    group = await create(service)
    db = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    token = set_session_id(group["group_id"])
    try:
        await db.initialize()
        await db.team.create_team(group["team_name"], "Group", "host")
        await db.member.create_member(
            "host", group["team_name"], "Host", "{}", "ready", role="leader"
        )
        backend = TeamBackend(group["team_name"], "host", True, db, AsyncMock())
        backend.conversation_mode = "group_chat"
        backend.bind_group_session(group["group_id"])
        backend.group_chat_spec = (
            await service.test_manager.get_swarm_enriched_team_spec(
                group["group_id"], request_metadata={"group_members": []}
            )
        )
        service.backend_resolver.return_value = backend
        await service.add_member(
            group["group_id"],
            {
                "member": dict(
                    kind="agent_group", id="group", member_name="research"
                )
            },
            user_id="owner",
            channel_id="web",
            request_id="add",
        )
        row = await db.member.get_member("research", group["team_name"])
        assert row.status == "unstarted"
        result = await deliver_group_message(
            backend, GroupChatMessage("Please research", "msg-1", mentions=["research"])
        )
        assert result.ok and result.data["notified_members"] == ["research"]
        assert "research" not in backend.group_chat_spec.agents
    finally:
        await db.close()
        reset_session_id(token)


@pytest.mark.asyncio
async def test_history_restores_public_messages_without_starting_runtime(service):
    group = await create(service)
    log = GroupConversationLog(group["team_name"], group["group_id"])
    messages = [
        ConversationMessage(
            message_id=str(n),
            team_name=group["team_name"],
            session_id=group["group_id"],
            client_message_id=f"m{n}",
            sender="user",
            sender_name="User",
            content=f"message {n}",
            timestamp=n,
            mentions=[],
            attachments=[],
        )
        for n in range(3)
    ]
    log.sync(messages)
    result = await service.history(
        group["group_id"], {"limit": 2}, user_id="owner", channel_id="web"
    )
    assert result["total"] == 3 and result["next_offset"] == 2
    assert [m["content"] for m in result["messages"]] == ["message 0", "message 1"]
    service.test_manager.get_swarm_enriched_team_spec.assert_awaited_once()
    assert (
        await service.history(
            group["group_id"], {"offset": 2}, user_id="owner", channel_id="web"
        )
    )["next_offset"] is None


@pytest.mark.asyncio
async def test_delete_cleans_private_runs_and_preserves_group_after_failure(
    service, monkeypatch
):
    group = await create(service)
    log = GroupConversationLog(group["team_name"], group["group_id"])
    runs = log.history_path.parent / "team-runs"
    runs.mkdir(parents=True)
    (runs / "one.json").write_text(
        json.dumps({"team_name": "group-expert-one", "session_id": "group-run-one"})
    )
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(gs.Runner, "delete_agent_team", remove)
    service.runtime.delete_session.return_value = SimpleNamespace(
        ok=False, error_code="FAILED", error_message="retry"
    )
    with pytest.raises(gs.GroupError, match="retry"):
        await service.delete(group["group_id"], user_id="owner", channel_id="web")
    assert gs.read_group(group["group_id"])["status"] == "ready"
    service.runtime.delete_session.return_value = SimpleNamespace(ok=True)
    assert (await service.delete(group["group_id"], user_id="owner", channel_id="web"))[
        "deleted"
    ]
    assert gs.read_group(group["group_id"]) is None
    remove.assert_awaited()


@pytest.mark.asyncio
async def test_delete_can_resume_after_process_left_deleting_marker(service):
    group = await create(service)
    state = gs.read_group(group["group_id"])
    state["status"] = "deleting"
    gs._save(state)
    assert (await service.delete(group["group_id"], user_id="owner", channel_id="web"))[
        "deleted"
    ]


@pytest.mark.asyncio
async def test_late_group_writer_cannot_recreate_deleted_session(service):
    group = await create(service)
    state = gs.read_group(group["group_id"])
    path = gs.group_path(group["group_id"])
    path.unlink()
    path.parent.rmdir()
    gs.lifecycle.guard.side_effect = gs.lifecycle.LifecycleError("NOT_FOUND", "deleted")
    with pytest.raises(gs.lifecycle.LifecycleError):
        gs._save(state)
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_corrupt_group_does_not_break_group_list(service):
    group = await create(service)
    gs.group_path(group["group_id"]).write_text("broken json")
    assert service.list({}, user_id="owner", channel_id="web")["total"] == 0
    with pytest.raises(gs.GroupError, match="invalid persisted"):
        service.get(group["group_id"], user_id="owner", channel_id="web")


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["../escape", "bad/slash", ""])
async def test_invalid_group_ids_cannot_access_files(service, group_id):
    with pytest.raises(ValueError):
        service.get(group_id, user_id="owner", channel_id="web")


@pytest.mark.asyncio
async def test_group_creation_marks_persistent_session_type(service):
    await create(service)
    assert gs.update_session_metadata.call_args.kwargs["conversation_type"] == "group_chat"


@pytest.mark.asyncio
async def test_create_ignores_model_project_and_reads_legacy_member_id(service):
    member = dict(kind="agent_template", id="one", member_name="advisor")
    group = await create(
        service,
        members=[member],
        model_name="ignored-model",
        project_id="ignored-project",
        project_dir="/ignored-project",
    )
    provision_input = service.runtime.prepare_session_create.await_args.args[0]
    assert provision_input.model_name == ""
    assert provision_input.project_id == ""
    assert provision_input.project_dir == ""
    assert group["members"] == [member]

    path = gs.group_path(group["group_id"])
    state = json.loads(path.read_text(encoding="utf-8"))
    legacy_member = dict(kind="expert", definition_id="one", member_name="advisor")
    state["members"] = [legacy_member]
    state["receipts"] = {"add-one": legacy_member}
    path.write_text(json.dumps(state), encoding="utf-8")
    restored = gs.read_group(group["group_id"])
    assert restored["members"] == [member]
    assert restored["receipts"]["add-one"] == member
    assert await create(service, members=[member]) == group


@pytest.mark.asyncio
async def test_create_configs_persist_and_participate_in_retry(service):
    configs = {"feature": True, "options": {"labels": ["one"]}}
    group = await create(service, configs=configs)
    assert group["configs"] == configs
    assert await create(service, configs=configs) == group
    assert service.get(group["group_id"], user_id="owner", channel_id="web")["configs"] == configs
    page = service.list({}, user_id="owner", channel_id="web")
    assert page["groups"][0]["configs"] == configs
    configs["options"]["labels"].append("two")
    assert gs.read_group(group["group_id"])["configs"]["options"]["labels"] == ["one"]
    with pytest.raises(gs.GroupError, match="different group"):
        await create(service, configs=configs)


@pytest.mark.asyncio
@pytest.mark.parametrize("configs", [None, [], "invalid", 1])
async def test_create_rejects_non_object_configs(service, configs):
    with pytest.raises(gs.GroupError, match="configs must be an object"):
        await create(service, configs=configs)
    service.runtime.prepare_session_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_group_defaults_configs_to_empty(service):
    group = await create(service)
    assert group["configs"] == {}
    path = gs.group_path(group["group_id"])
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["configs"]
    del state["avatar"]
    path.write_text(json.dumps(state), encoding="utf-8")
    assert service.get(group["group_id"], user_id="owner", channel_id="web")["configs"] == {}
    assert await create(service) == group


@pytest.mark.asyncio
async def test_group_avatar_create_update_clear_and_retry(service):
    avatar = "https://example.com/avatar.png"
    group = await create(service, avatar=avatar)
    assert group["avatar"] == avatar
    assert await create(service, avatar=avatar) == group
    with pytest.raises(gs.GroupError, match="different group"):
        await create(service, avatar="https://example.com/other.png")
    assert service.list({}, user_id="owner", channel_id="web")["groups"][0]["avatar"] == avatar
    updated = await service.update(
        group["group_id"], {"avatar": "https://example.com/new.png"}, user_id="owner", channel_id="web"
    )
    assert updated["avatar"] == "https://example.com/new.png"
    cleared = await service.update(
        group["group_id"], {"avatar": ""}, user_id="owner", channel_id="web"
    )
    assert cleared["avatar"] == ""
    assert service.get(group["group_id"], user_id="owner", channel_id="web")["avatar"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("avatar", [None, [], 42, "x" * 4097])
async def test_group_rejects_invalid_avatar(service, avatar):
    with pytest.raises(gs.GroupError, match="invalid avatar"):
        await create(service, avatar=avatar)
    group = await create(service)
    with pytest.raises(gs.GroupError, match="invalid avatar"):
        await service.update(group["group_id"], {"avatar": avatar}, user_id="owner", channel_id="web")
    assert service.get(group["group_id"], user_id="owner", channel_id="web")["avatar"] == ""


@pytest.mark.asyncio
async def test_create_and_add_members_without_count_limit(service):
    members = [
        {"kind": "agent_template", "id": "group", "member_name": f"expert-{index}"}
        for index in range(101)
    ]
    group = await create(service, members=members)
    assert len(group["members"]) == 101
    await service.add_member(
        group["group_id"],
        {"member": {"kind": "agent_template", "id": "group", "member_name": "another"}},
        user_id="owner",
        channel_id="web",
        request_id="add-over-limit",
    )
    assert len(service.get(group["group_id"], user_id="owner", channel_id="web")["members"]) == 102
