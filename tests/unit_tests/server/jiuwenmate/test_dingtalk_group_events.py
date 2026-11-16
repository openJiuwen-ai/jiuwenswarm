"""DingTalk Stream lifecycle facts update directories, never start Agent tasks."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_group_events import DingTalkGroupEvent
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkInboundHandler
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from tests.unit_tests.server.jiuwenmate.test_group_directory_attachment import History, directory_payload, invoke

NOW = datetime(2026, 11, 4, 0, tzinfo=timezone.utc)


def envelope(kind="chat_add_member", *, seconds=0, **changes):
    return {"eventType": kind, "eventId": "event-" + kind + str(seconds), "eventCorpId": "corp",
            "eventBornTime": int((NOW + timedelta(seconds=seconds)).timestamp() * 1000),
            "data": {"openConversationId": "cid-group", "timeStamp": int((NOW + timedelta(seconds=seconds)).timestamp() * 1000),
                     "corpId": "corp", "operator": "staff", **changes}}


def event(kind="chat_add_member", *, seconds=0, **changes):
    parsed = DingTalkGroupEvent.from_stream(envelope(kind, seconds=seconds, **changes), app_id="app", agent_id="agent")
    return replace(parsed, received_at=NOW + timedelta(seconds=seconds))


def host(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3")
    config = {"channels": {"dingtalk": {"apps": [
        {"enabled": True, "client_id": "app", "client_secret": "fake", "persistent_agent_id": "agent", "robot_code": "robot"},
    ]}}}
    service = SimpleNamespace(connection_of=lambda *a: SimpleNamespace(channel="dingtalk", connection_key="app"))
    runtime = SimpleNamespace(inspect_group_membership=AsyncMock(return_value={"status": "active"}))
    manager = SimpleNamespace(group_chats=groups, dingtalk_cli_runtime=runtime)
    inbound = DingTalkInboundHandler(service=service, sessions=MagicMock(), agent_manager=manager, config_loader=lambda: config)
    return SimpleNamespace(groups=groups, config=config, runtime=runtime, manager=manager, inbound=inbound)


def received_group(h, *, seconds=0):
    stamp = NOW + timedelta(seconds=seconds)
    group = h.groups.resolve("local", "dingtalk", "cid-group", now=stamp)
    h.groups.set_name("local", group.group_chat_id, "原群名")
    h.groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=stamp,
                            message_time=stamp, source_ref="dingtalk:message:first")
    return group


@pytest.mark.parametrize("kind", ["chat_add_member", "chat_remove_member", "chat_quit", "chat_update_title", "chat_disband"])
def test_official_stream_envelope_round_trip(kind):
    original = event(kind, title="New name")
    assert original.event_type == kind and original.conversation_id == "cid-group"
    assert original.changed_at == NOW
    assert DingTalkGroupEvent.from_payload(original.to_payload()) == original


def test_sdk_event_headers_with_business_data_are_supported():
    raw = envelope()
    headers = SimpleNamespace(event_type=raw["eventType"], event_id=raw["eventId"], event_corp_id="corp",
                              event_born_time=raw["eventBornTime"])
    parsed = DingTalkGroupEvent.from_stream(raw["data"], app_id="app", agent_id="agent", headers=headers)
    assert parsed.event_id == raw["eventId"]


@pytest.mark.parametrize("mutation", ["corp", "missing_time", "invalid_time", "missing_id", "missing_group", "conflict_type"])
def test_incomplete_or_conflicting_envelope_is_rejected(mutation):
    raw = envelope()
    headers = None
    if mutation == "corp": raw["data"]["corpId"] = "another-corp"
    if mutation == "missing_time": raw["data"].pop("timeStamp"); raw.pop("eventBornTime")
    if mutation == "invalid_time": raw["data"]["timeStamp"] = True
    if mutation == "missing_id": raw.pop("eventId")
    if mutation == "missing_group": raw["data"].pop("openConversationId")
    if mutation == "conflict_type": headers = SimpleNamespace(event_type="chat_disband")
    with pytest.raises(ValueError):
        DingTalkGroupEvent.from_stream(raw, app_id="app", agent_id="agent", headers=headers)


@pytest.mark.asyncio
async def test_positive_lifecycle_does_not_register_silent_group_before_first_message(tmp_path):
    h = host(tmp_path)
    assert await h.inbound.handle_group_change(event()) is InboundDisposition.HANDLED
    rows = h.groups.directory("local", "agent")
    assert rows == ()
    assert h.groups.active_accesses("local", "agent") == ()
    history = History()
    await invoke(h.groups, history, agent="agent")
    assert directory_payload(history.messages[-1])["directory"]["groups"] == []
    group = received_group(h)
    await invoke(h.groups, history, agent="agent")
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == [
        {"group_id": group.group_chat_id, "platform": "dingtalk", "name": "原群名", "status": "active"},
    ]
    h.inbound._sessions.resolve_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "unknown", "left"])
async def test_human_added_event_never_registers_a_group_without_a_message(tmp_path, status):
    h = host(tmp_path)
    h.runtime.inspect_group_membership.return_value = {"status": status}
    result = await h.inbound.handle_group_change(event(userId=["human"]))
    assert result is InboundDisposition.HANDLED
    assert h.groups.active_accesses("local", "agent") == ()
    assert h.groups.directory("local", "agent") == ()


@pytest.mark.asyncio
async def test_member_removal_does_not_remove_the_robot_when_only_a_human_left(tmp_path):
    h = host(tmp_path)
    group = received_group(h)
    await h.inbound.handle_group_change(event("chat_remove_member", seconds=1, userId=["human"]))
    assert h.groups.directory("local", "agent")[0]["status"] == "active"
    h.runtime.inspect_group_membership.return_value = {"status": "left"}
    await h.inbound.handle_group_change(event("chat_quit", seconds=2))
    assert h.groups.directory("local", "agent")[0]["status"] == "left"
    assert h.groups.active_accesses("local", "agent") == ()
    assert h.groups.access_for("local", "agent", group.group_chat_id).status == "left"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "left"])
async def test_positive_human_event_cannot_revive_retired_bot_access(tmp_path, status):
    h = host(tmp_path)
    group = received_group(h)
    h.groups.update_membership("local", "agent", group.group_chat_id, "app", status=status,
                               observed_at=NOW + timedelta(seconds=1), source_ref="event:retired")
    retired = h.groups.access_for("local", "agent", group.group_chat_id)
    assert await h.inbound.handle_group_change(event(seconds=2)) is InboundDisposition.HANDLED
    assert h.groups.active_accesses("local", "agent") == ()
    assert h.groups.access_for("local", "agent", group.group_chat_id) == retired


@pytest.mark.asyncio
async def test_rename_is_ordered_and_dissolution_reaches_existing_attachment(tmp_path):
    h = host(tmp_path)
    group = received_group(h)
    history = History()
    await invoke(h.groups, history, agent="agent")
    await h.inbound.handle_group_change(event("chat_update_title", seconds=2, title="新群名"))
    await h.inbound.handle_group_change(event("chat_update_title", seconds=1, title="迟到的旧群名"))
    await invoke(h.groups, history, agent="agent")
    assert "新群名" in history.messages[-1].content and "迟到的旧群名" not in history.messages[-1].content
    await h.inbound.handle_group_change(event("chat_disband", seconds=3))
    await invoke(h.groups, history, agent="agent")
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == []
    assert payload["changes"] == [{"event": "已移出", "group_id": group.group_chat_id, "name": "新群名"}]
    assert h.groups.active_accesses("local", "agent") == ()
    await h.inbound.handle_group_change(event("chat_disband", seconds=3))
    await invoke(h.groups, history, agent="agent")
    assert len(history.messages) == 3


@pytest.mark.asyncio
async def test_unknown_group_rename_and_stale_app_binding_do_not_create_access(tmp_path):
    h = host(tmp_path)
    await h.inbound.handle_group_change(event("chat_update_title", title="Unrelated group"))
    assert h.groups.directory("local", "agent") == ()
    h.config["channels"]["dingtalk"]["apps"][0]["persistent_agent_id"] = "other-agent"
    assert await h.inbound.handle_group_change(event()) is InboundDisposition.REJECTED
    h.runtime.inspect_group_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_lookup_failure_is_not_a_departure(tmp_path):
    h = host(tmp_path)
    received_group(h)
    h.runtime.inspect_group_membership.side_effect = RuntimeError("SECRET")
    result = await h.inbound.handle_group_change(event("chat_remove_member", seconds=1))
    assert result is InboundDisposition.REJECTED
    assert h.groups.directory("local", "agent")[0]["status"] == "active"
    h.runtime.inspect_group_membership.side_effect = None
    h.runtime.inspect_group_membership.return_value = {"status": "left"}
    await h.inbound.handle_group_change(event("chat_remove_member", seconds=2))
    assert h.groups.directory("local", "agent")[0]["status"] == "left"


@pytest.mark.asyncio
async def test_late_human_event_uses_current_probe_receipt_time(tmp_path):
    h = host(tmp_path)
    received_group(h, seconds=2)
    delayed = replace(event("chat_quit", seconds=1), received_at=NOW + timedelta(seconds=3))
    h.runtime.inspect_group_membership.return_value = {"status": "left"}
    await h.inbound.handle_group_change(delayed)
    assert h.groups.directory("local", "agent")[0]["status"] == "left"


@pytest.mark.asyncio
async def test_dissolution_before_first_message_never_grants_membership(tmp_path):
    h = host(tmp_path)
    await h.inbound.handle_group_change(event("chat_disband", seconds=2))
    assert h.groups.directory("local", "agent") == ()
    group = h.groups.resolve("local", "dingtalk", "cid-group", now=NOW)
    for seconds in (0, 3):
        stamp = NOW + timedelta(seconds=seconds)
        h.groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=stamp,
                                message_time=stamp, source_ref="dingtalk:message:after-dissolution")
        assert h.groups.active_accesses("local", "agent") == ()
        assert h.groups.directory("local", "agent") == ()


@pytest.mark.asyncio
async def test_dissolution_does_not_touch_a_same_id_feishu_group(tmp_path):
    h = host(tmp_path)
    feishu = h.groups.resolve("local", "feishu", "cid-group", now=NOW)
    h.groups.observe_access("local", "agent", feishu.group_chat_id, "feishu-app", observed_at=NOW,
                            message_time=NOW, source_ref="feishu:message:first")
    dingtalk = received_group(h)
    await h.inbound.handle_group_change(event("chat_disband", seconds=1))
    assert h.groups.access_for("local", "agent", feishu.group_chat_id).status == "active"
    assert h.groups.access_for("local", "agent", dingtalk.group_chat_id).status == "left"


@pytest.mark.asyncio
async def test_lifecycle_flows_through_gateway_and_agentserver_without_chat_dispatch(monkeypatch):
    import asyncio
    from jiuwenswarm.server import agent_ws_server as module
    from agent_gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request

    lifecycle = event("chat_disband")
    inbound = SimpleNamespace(handle=AsyncMock(), handle_group_change=AsyncMock(return_value=InboundDisposition.HANDLED))
    server = object.__new__(module.AgentWebSocketServer)
    server._ensure_persistent_group_fanout = AsyncMock(return_value=SimpleNamespace(dingtalk_inbound=inbound))
    wire = AsyncMock()
    monkeypatch.setattr(module, "send_wire_payload", wire)
    handler = object.__new__(MessageHandler)

    async def send(envelope):
        await server.dispatch_parsed_request(object(), e2a_to_agent_request(envelope), asyncio.Lock())
        return SimpleNamespace(ok=True, payload={"disposition": "handled"})

    handler.agent_client = SimpleNamespace(send_request=send)
    assert (await handler.handle_persistent_dingtalk_event(lifecycle)).value == "handled"
    assert inbound.handle_group_change.await_args.args[0] == lifecycle
    inbound.handle.assert_not_awaited()
    wire.assert_awaited_once()
