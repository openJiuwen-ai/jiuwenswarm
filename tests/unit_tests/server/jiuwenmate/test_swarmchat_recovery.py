"""Colleague availability, incremental mention recovery and additive group stop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole


def _message(message_id: str, timestamp: int, *, sender: str = "user", mentions=("researcher",)):
    return ConversationMessage(
        message_id=message_id, team_name="team", session_id="b-good", client_message_id=message_id,
        sender=sender, sender_name=sender, content=f"text {message_id}", timestamp=timestamp,
        mentions=list(mentions), attachments=[],
    )


def _group(group_id: str) -> dict:
    return {
        "group_id": group_id, "session_id": group_id, "team_name": "team", "user_id": "owner",
        "channel_id": "web", "status": "ready", "created_at": 0,
        "members": [{"kind": "colleague", "id": "mate", "member_name": "researcher"}],
        "compiled": {"researcher": {"joined_at": 0}},
    }


def _forbid_colleague_lookup(monkeypatch):
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService

    def lookup(cls):
        raise AssertionError("colleague availability must not be checked while building the group")

    monkeypatch.setattr(PersistentAgentService, "create_default", classmethod(lookup))


def test_saved_colleague_builds_without_checking_availability(monkeypatch):
    from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
    from jiuwenswarm.agents.swarm import group_chat

    _forbid_colleague_lookup(monkeypatch)
    monkeypatch.setattr(group_chat, "declare_group_cli_kinds", lambda spec: None)
    spec = TeamAgentSpec(agents={"leader": DeepAgentSpec()}, team_name="team", leader=LeaderSpec(member_name="leader"))
    group_chat.assemble_group_members(spec, [{
        "kind": "colleague", "id": "deleted-mate", "member_name": "researcher", "display_name": "Researcher",
    }])
    member = spec.predefined_members[-1]
    assert member.role_type == TeamRole.HOST_MANAGED
    assert member.display_name == "Researcher"
    assert "researcher" not in spec.agents


async def test_compile_keeps_joined_colleague_and_rejects_new_unavailable_one(monkeypatch):
    from jiuwenswarm.agents.swarm import group_chat
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
    from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
    from jiuwenswarm.server.runtime.group_service import GroupError, GroupService

    monkeypatch.setattr(group_chat, "enabled_group_cli_kinds", lambda: set())
    host = TeamMemberSpec(member_name="researcher", display_name="Researcher", desc="", prompt="",
                          role_type=TeamRole.HOST_MANAGED)
    manager = SimpleNamespace(get_swarm_enriched_team_spec=AsyncMock(
        return_value=SimpleNamespace(predefined_members=[host], agents={}),
    ))
    service = GroupService(None, manager_factory=lambda channel: manager)
    members = [{"kind": "colleague", "id": "mate", "member_name": "researcher", "display_name": "Researcher"}]
    joined = {"member": host.model_dump(mode="json"), "template": None, "joined_at": 1.0}
    state = {"session_id": "g", "mode": "team", "user_id": "owner", "channel_id": "web",
             "compiled": {"researcher": joined}}

    _forbid_colleague_lookup(monkeypatch)
    _, compiled = await service._compile(state, members)
    assert compiled["researcher"] is joined

    state["compiled"] = {}
    monkeypatch.setattr(PersistentAgentService, "create_default", classmethod(lambda cls: SimpleNamespace(
        get_colleague=lambda *args: SimpleNamespace(enabled=False),
    )))
    with pytest.raises(GroupError) as disabled:
        await service._compile(state, [dict(members[0])])
    assert disabled.value.code == "BAD_REQUEST"

    def missing(*args):
        raise PersistentAgentError("colleague not found", code="NOT_FOUND")

    monkeypatch.setattr(PersistentAgentService, "create_default",
                        classmethod(lambda cls: SimpleNamespace(get_colleague=missing)))
    with pytest.raises(GroupError) as deleted:
        await service._compile(state, [dict(members[0])])
    assert deleted.value.code == "BAD_REQUEST"


async def test_colleague_history_read_does_not_rewrite_the_projection(monkeypatch):
    from openjiuwen.agent_teams.group_chat import handler
    from jiuwenswarm.server.runtime import group_service

    rows = [_message("m2", 1_700_000_002_000), _message("m1", 1_700_000_000_000)]
    backend = SimpleNamespace(team_name="team", db=SimpleNamespace(
        initialize=AsyncMock(), message=SimpleNamespace(get_team_messages=AsyncMock(return_value=rows)),
    ))

    async def rewrite(*args):
        raise AssertionError("history.jsonl must not be rewritten by colleague reads")

    monkeypatch.setattr(group_service, "sync_history", rewrite)
    monkeypatch.setattr(handler, "group_metadata", lambda row: {"type": "group_chat"})
    monkeypatch.setattr(handler, "conversation_message", lambda row, session_id: row)
    state = dict(_group("b-good"), history_cleared_at=1_700_000_001.0)
    monkeypatch.setattr(group_service.GroupService, "owned", lambda self, *args: state)
    service = group_service.GroupService(None, backend_resolver=AsyncMock(return_value=backend))
    messages = await service.public_messages("b-good", user_id="owner", channel_id="web")
    assert [item.message_id for item in messages] == ["m2"]


def _bridge(module, tmp_path, monkeypatch, groups: dict, backends: dict, history: list):
    sessions = tmp_path / "sessions"
    for group_id in groups:
        (sessions / group_id).mkdir(parents=True)
        (sessions / group_id / "group.json").write_text("{}")
    monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(module, "read_group", lambda group_id: groups[group_id])
    monkeypatch.setattr(module, "resolve_group_backend", AsyncMock(side_effect=lambda group: backends[group["group_id"]]))
    reads = AsyncMock(return_value=history)
    monkeypatch.setattr(module.GroupService, "public_messages", reads)
    manager = SimpleNamespace(db_path=tmp_path / "agents.sqlite3",
                              accept_swarmchat_message=AsyncMock(), reconcile_swarmchat_requests=AsyncMock())
    return module.ColleagueGroupBridge(manager), manager, reads


async def test_scan_isolates_broken_groups_and_skips_handled_mentions(tmp_path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import bridge as module

    good = SimpleNamespace(register_host_managed_inbound=AsyncMock())
    broken = SimpleNamespace(register_host_managed_inbound=AsyncMock(side_effect=KeyError("researcher")))
    history = [_message("m1", 1), _message("m2", 2, mentions=())]
    bridge, manager, _ = _bridge(module, tmp_path, monkeypatch,
                                 {"a-broken": _group("a-broken"), "b-good": _group("b-good")},
                                 {"a-broken": broken, "b-good": good}, history)
    try:
        await bridge.catch_up()
        assert manager.accept_swarmchat_message.await_count == 1
        good.register_host_managed_inbound.assert_awaited_once()

        submit = AsyncMock(return_value=True)
        monkeypatch.setattr(bridge, "submit", submit)
        await bridge.catch_up()
        submit.assert_not_awaited()
        assert good.register_host_managed_inbound.await_count == 1
    finally:
        await bridge.aclose()


async def test_callback_returns_at_once_and_scan_waits_for_it(tmp_path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import bridge as module

    backend = SimpleNamespace(register_host_managed_inbound=AsyncMock())
    message = _message("m1", 1)
    bridge, manager, _ = _bridge(module, tmp_path, monkeypatch, {"b-good": _group("b-good")},
                                 {"b-good": backend}, [message])
    release = asyncio.Event()

    async def slow_accept(*args):
        await release.wait()

    manager.accept_swarmchat_message.side_effect = slow_accept
    try:
        await bridge._receive("b-good", "researcher")(message)
        await asyncio.sleep(0)
        await bridge.catch_up()
        assert bridge._consumption.watermark("b-good", "researcher") is None

        release.set()
        await asyncio.gather(*bridge._pending)
        await bridge.catch_up()
        assert manager.accept_swarmchat_message.await_count == 1
        assert bridge._consumption.watermark("b-good", "researcher") == (1, "m1")
    finally:
        await bridge.aclose()


async def test_group_stop_continues_when_colleague_stop_fails(monkeypatch):
    from jiuwenswarm.server import agent_ws_server
    from jiuwenswarm.server.runtime import group_service

    monkeypatch.setattr(group_service, "read_group", lambda group_id: _group(group_id))
    expected = SimpleNamespace(ok=True, payload={})
    server = SimpleNamespace(
        _jiuwenmate_web=SimpleNamespace(stop_swarmchat_group=AsyncMock(side_effect=RuntimeError("down"))),
        _cancel_organization=AsyncMock(return_value=expected),
    )
    request = SimpleNamespace(params={"organization_stop": True}, session_id="b-good",
                              channel_id="web", user_id="", request_id="request")
    response = await agent_ws_server.AgentWebSocketServer._handle_cancel(
        server, None, request, asyncio.Lock(), send_response=False,
    )
    assert response is expected
    server._jiuwenmate_web.stop_swarmchat_group.assert_awaited_once()
    server._cancel_organization.assert_awaited_once_with(request)

    server._jiuwenmate_web.stop_swarmchat_group.reset_mock()
    corrupt = Mock(side_effect=group_service.GroupError("GROUP_CORRUPT", "invalid persisted group"))
    monkeypatch.setattr(group_service, "read_group", corrupt)
    response = await agent_ws_server.AgentWebSocketServer._handle_cancel(
        server, None, request, asyncio.Lock(), send_response=False,
    )
    assert response is expected
    assert server._cancel_organization.await_count == 2
    server._jiuwenmate_web.stop_swarmchat_group.assert_not_awaited()

    monkeypatch.setattr(group_service, "read_group", lambda group_id: dict(_group(group_id), members=[]))
    server._jiuwenmate_web.stop_swarmchat_group.reset_mock()
    await agent_ws_server.AgentWebSocketServer._handle_cancel(server, None, request, asyncio.Lock(),
                                                              send_response=False)
    server._jiuwenmate_web.stop_swarmchat_group.assert_not_awaited()
