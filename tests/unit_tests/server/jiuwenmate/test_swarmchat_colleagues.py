"""Host-managed roster, authorized history and durable request routing."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.schema.team import TeamRole, TeamMemberSpec
from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef, RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.swarmchat.port import SwarmChatGroupChatPort


async def test_host_role_registers_without_hitt_and_rejects_duplicate():
    token = set_session_id("host-role")
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    try:
        await db.initialize()
        backend = TeamBackend(team_name="host_team", member_name="team_leader", is_leader=True,
                              db=db, messager=AsyncMock(), enable_hitt=False)
        await backend.build_team(display_name="Host team", desc="", leader_display_name="Leader", leader_desc="")
        result = await backend.spawn_host_managed_member(member_name="researcher", display_name="Researcher")
        assert result.ok
        member = await db.member.get_member("researcher", "host_team")
        assert member.role == "host_managed"
        assert member.status == "ready"
        assert not await backend.is_human_agent("researcher")
        assert not (await backend.spawn_host_managed_member(
            member_name="researcher", display_name="Other",
        )).ok
        callback = AsyncMock()
        await backend.register_host_managed_inbound("researcher", callback)
        message = SimpleNamespace(mentions=["researcher"], sender="user")
        await backend.notify_host_managed_inbound(message)
        callback.assert_awaited_once_with(message)
        await backend.register_host_managed_inbound("researcher", None)
        await backend.notify_host_managed_inbound(message)
        assert callback.await_count == 1
        assert (await backend.shutdown_member("researcher")).ok
        assert (await db.member.get_member("researcher", "host_team")).status == "shut_down"
    finally:
        await db.close()
        reset_session_id(token)


def test_swarm_session_and_delivery_identity_are_distinct_from_im():
    conversation = ConversationRef(kind="swarm_group", channel="swarmchat", key="same-group-id")
    assert conversation.accepts("group_message")
    assert conversation != ConversationRef.group("same-group-id")
    assert RequestRouteSnapshot(channel="swarmchat", target_id="group").channel == "swarmchat"
    assert TeamMemberSpec(member_name="researcher", display_name="Researcher", desc="AI", prompt="",
                          role_type=TeamRole.HOST_MANAGED).role_type == TeamRole.HOST_MANAGED


async def test_recent_background_has_four_messages_without_duplicate_trigger(monkeypatch):
    group = {"group_id": "group", "name": "Group", "user_id": "local", "members": [
        {"kind": "colleague", "id": "mate", "member_name": "researcher"},
    ]}
    monkeypatch.setattr('jiuwenswarm.server.jiuwenmate.swarmchat.port.require_membership',
                        lambda *args: (group, group["members"][0]))
    service = SimpleNamespace(get_colleague=lambda *args: SimpleNamespace(enabled=True),
                              connection_of=lambda *args: None)
    sessions = SimpleNamespace(
        get_session=lambda *args: SimpleNamespace(conversation=ConversationRef(
            kind="swarm_group", channel="swarmchat", key="group")),
        get_request=lambda *args: SimpleNamespace(record=SimpleNamespace(
            request_input=SimpleNamespace(trigger_message_id="m9"))),
    )
    port = SwarmChatGroupChatPort(sessions, service, None).bind("local", "mate")
    messages = [ConversationMessage(message_id=f"m{i}", team_name="team", session_id="group",
                client_message_id=f"c{i}", sender="user", sender_name="User", content="x" * 3000,
                timestamp=1700000000000 + i, mentions=[], attachments=[]) for i in range(10)]
    port.messages = AsyncMock(return_value=messages)
    from openjiuwen.harness.persistent_agent import ExecutionContext

    execution = ExecutionContext(namespace_id="local", agent_id="mate", session_id="session",
        request_id="request", execution_id="execution", source_ref="source", config_version="v1", config_fingerprint="a" * 64,
        principal_ref="local", startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}), policy_revision="p1",
        capability_snapshot=frozenset())
    result = await port.recent_messages(execution)
    assert [item.message_id for item in result.messages] == ["m5", "m6", "m7", "m8"]
    assert all(len(item.text) == 2000 for item in result.messages)
    assert result.scanned_through == "m9"


def test_automatic_budget_is_persistent_atomic_and_stoppable(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from jiuwenswarm.server.jiuwenmate.swarmchat.consumption import SwarmChatConsumption

    store = SwarmChatConsumption(tmp_path / "agents.sqlite3")
    def reserve(index):
        return store.reserve("group", f"m{index}", "mate", "root", True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(20)))
    assert results.count("reserved") == 5
    assert results.count("limited") == 15
    restored = SwarmChatConsumption(store.db_path)
    assert restored.reserve("group", "new", "mate", "root", True) == "limited"
    restored.stop("group", "root")
    assert restored.reserve("group", "another", "mate", "root", False) == "stopped"
    assert restored.reserve("group", "next", "mate", "new-user-message", True) == "reserved"


async def test_search_reads_other_authorized_swarm_groups_and_paginates(tmp_path, monkeypatch):
    from openjiuwen.harness.persistent_agent import ExecutionContext, GroupMessageQuery
    from jiuwenswarm.server.jiuwenmate.group_search_cursor_repository import GroupSearchCursorRepository
    from jiuwenswarm.server.jiuwenmate.swarmchat import port as module

    groups = [{"group_id": name, "name": name, "user_id": "owner", "channel_id": "web",
               "members": [{"kind": "colleague", "id": "mate", "member_name": "researcher"}]}
              for name in ("group-a", "group-b")]
    monkeypatch.setattr(module, "groups_for", lambda *args, **kwargs: groups)
    monkeypatch.setattr(module, "require_membership", lambda group_id, agent_id: (
        next(item for item in groups if item["group_id"] == group_id), groups[0]["members"][0],
    ))
    sessions = SimpleNamespace(db_path=tmp_path / "agents.sqlite3", get_session=lambda *args: SimpleNamespace(
        conversation=ConversationRef(kind="swarm_group", channel="swarmchat", key="group-a")))
    service = SimpleNamespace(get_colleague=lambda *args: SimpleNamespace(enabled=True),
                              connection_of=lambda *args: None)
    port = SwarmChatGroupChatPort(sessions, service, GroupSearchCursorRepository(sessions.db_path)).bind("local", "mate")
    def messages(group):
        index = 1 if group["group_id"] == "group-a" else 2
        return [ConversationMessage(message_id=f"m{index}", team_name="team", session_id=group["group_id"],
                client_message_id=f"c{index}", sender="user", sender_name="User", content=f"plan {index}",
                timestamp=1700000000000 + index, mentions=[], attachments=[])]
    port.messages = AsyncMock(side_effect=messages)
    execution = ExecutionContext(namespace_id="local", agent_id="mate", session_id="session",
        request_id="request", execution_id="execution", source_ref="source", config_version="v1",
        config_fingerprint="a" * 64, principal_ref="owner", startup_scope=frozenset({"chat"}),
        source_scope=frozenset({"chat"}), policy_revision="p1", capability_snapshot=frozenset())
    query = GroupMessageQuery(platforms=("swarmchat",), content_types=("messages",), query="plan", limit=1)
    first = await port.search_messages(execution, query)
    assert first.hits[0].group_id == "group-b"
    assert first.status == "incomplete" and first.next_cursor
    second = await port.search_messages(execution, query.model_copy(update={"cursor": first.next_cursor}))
    assert second.hits[0].group_id == "group-a"
    assert second.query_complete
    unavailable = await port.search_messages(execution, query.model_copy(update={"group_ids": ("not-a-member",)}))
    assert unavailable.status == "unavailable" and not unavailable.hits


async def test_persisted_mentions_recover_once_and_reply_as_the_colleague(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.paths import set_task_openjiuwen_home, reset_task_openjiuwen_home
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec, LeaderSpec
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
    from jiuwenswarm.server.jiuwenmate.swarmchat import bridge as module
    from jiuwenswarm.server.jiuwenmate.swarmchat import delivery
    from jiuwenswarm.server.runtime import group_execution

    token = set_session_id("swarm-group")
    home = set_task_openjiuwen_home(str(tmp_path / "core"))
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    bridge = None
    try:
        await db.initialize()
        await db.create_cur_session_tables()
        backend = TeamBackend("team", "leader", True, db, AsyncMock())
        backend.conversation_mode = "group_chat"
        backend.group_chat_spec = TeamAgentSpec(agents={"leader": DeepAgentSpec()}, team_name="team",
                                                leader=LeaderSpec(member_name="leader"))
        backend.bind_group_session("swarm-group")
        await backend.build_team(display_name="Group", desc="", leader_display_name="Leader", leader_desc="")
        assert (await backend.spawn_host_managed_member(member_name="researcher", display_name="Researcher")).ok
        state = {"group_id": "swarm-group", "session_id": "swarm-group", "team_name": "team",
                 "user_id": "owner", "channel_id": "web", "status": "ready", "created_at": 0,
                 "members": [{"kind": "colleague", "id": "mate", "member_name": "researcher"}],
                 "compiled": {"researcher": {"joined_at": 0}}}
        session_dir = tmp_path / "sessions"
        (session_dir / "swarm-group").mkdir(parents=True)
        (session_dir / "swarm-group" / "group.json").write_text("{}")
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: session_dir)
        monkeypatch.setattr(module, "read_group", lambda group_id: state)
        monkeypatch.setattr(module, "resolve_group_backend", AsyncMock(return_value=backend))
        manager = SimpleNamespace(db_path=tmp_path / "agents.sqlite3",
                                  accept_swarmchat_message=AsyncMock(), reconcile_swarmchat_requests=AsyncMock())
        bridge = module.ColleagueGroupBridge(manager)
        async def history(*args, **kwargs):
            from openjiuwen.agent_teams.group_chat.handler import sync_history
            return await sync_history(await backend.group_conversation(), backend.message_manager)
        monkeypatch.setattr(module.GroupService, "public_messages", history)
        await bridge.catch_up()
        posted = await backend.append_group_message("user", "Research plan", client_message_id="input-1",
                                                    mentions=["researcher"])
        assert posted.notified_members == []
        assert await db.message.get_unread_group_members("team") == []
        await backend.notify_host_managed_inbound(posted.message)
        await asyncio.gather(*bridge._pending)
        assert manager.accept_swarmchat_message.await_count == 1
        await bridge.catch_up()
        assert manager.accept_swarmchat_message.await_count == 1
        accepted = manager.accept_swarmchat_message.call_args.args[2]
        assert accepted.mention_ids == ("mate",)
        await bridge.aclose()
        bridge = module.ColleagueGroupBridge(manager)
        await bridge.catch_up()
        assert manager.accept_swarmchat_message.await_count == 1
        monkeypatch.setattr(delivery, "require_membership", lambda *args: (state, state["members"][0]))
        monkeypatch.setattr(delivery, "resolve_group_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(group_execution, "project_group_stream_event", AsyncMock(return_value=None))
        service = SimpleNamespace(get_colleague=lambda *args: SimpleNamespace(enabled=True))
        job = {"namespace_id": "local", "agent_id": "mate", "target_id": "swarm-group",
               "delivery_id": "delivery-1", "request_id": "request-1", "idempotency_key": "reply-1",
               "text": "The plan is ready."}
        first = await delivery.send_delivery(job, service=service)
        second = await delivery.send_delivery(job, service=service)
        assert first["outcome"] == "delivered"
        assert first["external_message_id"] == second["external_message_id"]
        messages = await history()
        assert len(messages) == 2
        assert messages[-1].sender == "researcher"
        assert messages[-1].content == "The plan is ready."
    finally:
        if bridge is not None:
            await bridge.aclose()
        await db.close()
        reset_task_openjiuwen_home(home)
        reset_session_id(token)


def test_group_file_import_reuses_session_boundaries_and_revokes_private_copy(tmp_path, monkeypatch):
    import hashlib
    import os
    from openjiuwen.harness.persistent_agent import ExecutionContext
    from jiuwenswarm.server.jiuwenmate.swarmchat import files, membership
    from jiuwenswarm.server.jiuwenmate.swarmchat.consumption import SwarmChatFileImports
    from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WorkspaceStore
    from jiuwenswarm.server.jiuwenmate.group_share_repository import GroupShareRepository
    from jiuwenswarm.server.jiuwenmate.group_sharing_service import GroupSharingService

    store = WorkspaceStore(tmp_path / "workspaces")
    source = tmp_path / "published.txt"
    source.write_text("shared group report")
    group = {"group_id": "group-a", "name": "A", "members": []}
    active = True
    def check(*args):
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
        if not active:
            raise PersistentAgentError("membership lost", code="FORBIDDEN")
        return group, {}
    monkeypatch.setattr(files, "require_membership", check)
    monkeypatch.setattr(membership, "require_membership", check)
    monkeypatch.setattr(files, "published_files", lambda group: [{"path": source}])
    execution = ExecutionContext(namespace_id="local", agent_id="mate", session_id="session",
        request_id="request", execution_id="execution", source_ref="source", config_version="v1",
        config_fingerprint="a" * 64, principal_ref="owner", startup_scope=frozenset({"workspace_read"}),
        source_scope=frozenset({"workspace_read"}), policy_revision="p1", capability_snapshot=frozenset())
    source_session, name, digest, imported = files.import_file(
        store, execution, "mate", group,
        {"path": source, "name": source.name, "size": source.stat().st_size},
    )
    assert source_session == execution.session_id
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    if os.name == "posix":
        staged = store.path("local", "mate") / "sessions" / source_session / "output" / name
        assert staged.stat().st_mode & 0o077 == 0
    target = store.path("local", "mate") / imported
    assert target.read_text() == "shared group report"
    imports = SwarmChatFileImports(tmp_path / "agents.sqlite3")
    imports.record(execution, imported, "group-a", str(source), digest)
    service = SimpleNamespace(workspaces=store, connection_of=lambda *args: None)
    sharing = GroupSharingService(service=service, sessions=SimpleNamespace(), groups=SimpleNamespace(),
        archive=SimpleNamespace(), repository=GroupShareRepository(imports.db_path))
    assert sharing.public_group_imports(execution) == (imported,)
    active = False
    assert sharing.public_group_imports(execution) == ()
    assert not target.exists()
    assert source.read_text() == "shared group report"


def test_directory_unifies_only_enabled_identities_for_the_same_colleague(monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import membership

    colleague = SimpleNamespace(agent_id="mate", enabled=True)
    service = SimpleNamespace(get_config=lambda *args: colleague, list_agents=lambda *args: [
        SimpleNamespace(config_id="mate", agent_id="feishu-mate", enabled=True),
        SimpleNamespace(config_id="mate", agent_id="disabled", enabled=False),
        SimpleNamespace(config_id="other", agent_id="other", enabled=True),
    ])
    repository = SimpleNamespace(directory=Mock(return_value=({
        "platform": "feishu", "group_id": "im-group", "name": "IM", "status": "active",
    },)))
    owner_filter = Mock(return_value=[{"group_id": "swarm-group", "name": "Swarm"}])
    monkeypatch.setattr(membership, "groups_for", owner_filter)
    directory = membership.ColleagueGroupDirectory(repository, service, owner="owner")
    rows = directory.directory("local", "mate")
    assert {(row["platform"], row["group_id"]) for row in rows} == {
        ("feishu", "im-group"), ("swarmchat", "swarm-group"),
    }
    repository.directory.assert_called_once_with("local", "feishu-mate")
    owner_filter.assert_called_once_with("mate", owner="owner")
    colleague.enabled = False
    assert directory.directory("local", "mate") == ()


@pytest.mark.parametrize("im_search_method", ["search_content", "search_messages"])
@pytest.mark.parametrize("platforms", [("swarmchat", "feishu"), ("feishu",)])
async def test_combined_search_preserves_explicit_im_platform_filter(monkeypatch, im_search_method, platforms):
    from openjiuwen.harness.persistent_agent import ExecutionContext, GroupMessageQuery, GroupMessageSearchResult
    from openjiuwen.harness.persistent_agent import InvocationRef
    from jiuwenswarm.server.jiuwenmate.swarmchat import port as module

    execution = ExecutionContext(namespace_id="local", agent_id="mate", session_id="session",
        request_id="request", execution_id="execution", source_ref="source", config_version="v1",
        config_fingerprint="a" * 64, principal_ref="owner", startup_scope=frozenset({"chat"}),
        source_scope=frozenset({"chat"}), policy_revision="p1", capability_snapshot=frozenset())
    result = GroupMessageSearchResult(**InvocationRef.from_execution(execution).model_dump(),
                                      status="empty", query_complete=True)
    im_search = AsyncMock(return_value=result)
    im = SimpleNamespace(**{im_search_method: im_search})
    swarm = SimpleNamespace(scope=("local", "mate"), validate_execution=lambda execution: "mate",
                            search_content=AsyncMock(return_value=result))
    monkeypatch.setattr(module, "groups_for", lambda *args: [])
    combined = module.CompositeGroupChatPort(im, swarm, None, None)
    query = GroupMessageQuery(platforms=platforms, content_types=("messages",), query="plan")
    found = await combined.search_messages(execution, query)
    assert found.status == "empty"
    assert im_search.await_args.args[1].platforms == ("feishu",)
    if "swarmchat" in platforms:
        assert swarm.search_content.await_args.args[1].platforms == ("swarmchat",)
    else:
        swarm.search_content.assert_not_called()


@pytest.mark.parametrize("previous_status", ["pending", "sending", "unknown"])
async def test_swarmchat_outbox_delivers_without_an_im_application_id(tmp_path, previous_status):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.execution_models import build_execution_context
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
    from tests.unit_tests.server.jiuwenmate.test_commit_port import ACCESS, AGENT, NOW, NS, SNAPSHOT, final

    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    session = repository.resolve_session(NS, AGENT, ConversationRef(
        kind="swarm_group", channel="swarmchat", key="group"), now=NOW)
    state, _ = repository.accept_request(session, snapshot=SNAPSHOT, source_ref="mention", access=ACCESS,
        request_input=core.RequestInput(kind="group_message", text="Hello", reply_target="group",
            source=core.GroupMessage(message_id="source", sender_id="user", sender_name="User",
                                     sender_kind="human", received_at=NOW, text="Hello")),
        received_at=NOW, route_snapshot=RequestRouteSnapshot(channel="swarmchat", target_id="group"))
    record = repository.start_execution(NS, state.record.request_id, now=NOW)
    execution = build_execution_context(state.record, record, capability_snapshot=SNAPSHOT.enabled_features)
    proposed = core.AgentEvent.create(execution, event_type=core.EventType.OUTPUT_PROPOSED, local_seq=1,
        occurred_at=NOW, visibility="candidate", payload={
            "output_id": "out-1", "target_ref": "group", "share_mode": "message", "text": "Hello back"})
    repository.record_events([proposed, final(execution, 2)], now=NOW)
    channels = frozenset({"swarmchat"})
    claimed = None
    if previous_status != "pending":
        claimed, = repository.claim_pending_result_deliveries(NS, channels=channels, now=NOW)
        if previous_status == "unknown":
            repository.update_result_delivery(
                NS, claimed["delivery_id"], outcome="unknown", external_message_id=None,
                error_code="ACK_TIMEOUT", now=NOW,
            )
    repository = ExecutionRepository(repository.db_path)
    assert repository.recover_sending_result_deliveries(NS, channels=channels, now=NOW) == (
        1 if previous_status == "sending" else 0
    )

    async def send(job):
        assert job["agent_id"] == AGENT and job["session_id"] == session.session_id
        assert job["app_id"] is None and job["text"] == "Hello back"
        if claimed is not None:
            assert job["idempotency_key"] == claimed["idempotency_key"]
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered",
                "external_message_id": "public-reply"}
    sender = AsyncMock(side_effect=send)
    outbox = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({"swarmchat"}),
                                   send_delivery=sender, clock=lambda: NOW)
    count = 0 if previous_status == "unknown" else 1
    assert await outbox.dispatch_pending() == count
    assert sender.await_count == count
    if previous_status == "unknown":
        row, = repository.result_deliveries(NS, execution.request_id)
        assert row["status"] == "unknown"
    assert await outbox.dispatch_pending() == 0
