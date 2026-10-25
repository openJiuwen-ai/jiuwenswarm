"""JM-08 production binding and Gateway-to-AgentServer wiring tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import (
    FeishuBinding,
    FeishuInboundEvent,
    InboundDisposition,
    configured_feishu_bindings,
    create_default_persistent_group_fanout,
    resolve_configured_feishu_bindings,
    resolve_feishu_access,
)


NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_group_gateway_membership_lookup_uses_exact_bound_app():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    channel.inspect_persistent_group = AsyncMock(return_value={"status": "active", "group_name": "Project"})
    manager.register_channel(channel)
    result = await manager.inspect_feishu_group({"app_id": "cli_bound", "chat_id": "oc_group"})
    assert result == {"status": "active", "group_name": "Project"}
    missing = await manager.inspect_feishu_group({"app_id": "other", "chat_id": "oc_group"})
    assert missing["status"] == "unknown"


@pytest.mark.asyncio
async def test_group_membership_push_returns_a_correlated_response():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    handler = object.__new__(MessageHandler)
    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True)))
    handler._feishu_group_handler = AsyncMock(return_value={"status": "left"})
    payload = {"event_type": "jiuwenmate.group.inspect", "query_id": "query-1",
               "app_id": "cli_bound", "chat_id": "oc_group"}
    await handler._handle_agent_server_push(build_server_push_wire({
        "request_id": "query-1", "channel_id": "feishu", "payload": payload,
    }))
    await asyncio.gather(*handler._feishu_group_inspect_tasks)
    envelope = handler.agent_client.send_request.await_args.args[0]
    assert envelope.method == "jiuwenmate.group.response"
    assert envelope.params["query_id"] == "query-1" and envelope.params["status"] == "left"


@pytest.mark.asyncio
async def test_group_inspect_returns_the_push_lock_before_feishu_responds():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    handler = object.__new__(MessageHandler)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_payload):
        started.set()
        await release.wait()
        return {"status": "active"}

    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True)))
    handler._feishu_group_handler = slow
    payload = {"event_type": "jiuwenmate.group.inspect", "query_id": "query-slow",
               "app_id": "cli_bound", "chat_id": "oc_group"}
    push = asyncio.create_task(handler._handle_agent_server_push(build_server_push_wire({
        "request_id": "query-slow", "channel_id": "feishu", "payload": payload,
    })))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(push, timeout=1)
    release.set()
    await asyncio.gather(*handler._feishu_group_inspect_tasks)
    handler.agent_client.send_request.assert_awaited()


@pytest.mark.asyncio
async def test_group_server_rejects_wrong_app_response_and_cleans_up_query(monkeypatch):
    server = object.__new__(_agent_websocket_server_type())
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", AsyncMock())

    async def push(message):
        params = message["payload"]
        query_id = params["query_id"]
        response = SimpleNamespace(request_id="response", channel_id="feishu",
            params={"query_id": query_id, "app_id": "wrong", "chat_id": "oc_group", "status": "active"})
        await server._handle_feishu_group_response(object(), response, asyncio.Lock())
        assert not server._feishu_group_waiters[query_id][0].done()
        response.params["app_id"] = "cli_bound"
        await server._handle_feishu_group_response(object(), response, asyncio.Lock())
        return True

    server.send_push = push
    assert (await server._inspect_feishu_group("cli_bound", "oc_group"))["status"] == "active"
    assert server._feishu_group_waiters == {}
    server.send_push = AsyncMock(return_value=False)
    assert (await server._inspect_feishu_group("cli_bound", "oc_group"))["status"] == "unknown"
    assert server._feishu_group_waiters == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("message_ids,channel_status,expected", [
    ((), "active", "active"), (("m1",), "ok", "ok"),
    ((), "ok", "unknown"), (("m1",), "active", "unknown"),
])
async def test_group_query_crosses_gateway_forwarding_and_returns_to_server(
    monkeypatch, message_ids, channel_status, expected,
):
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    server = object.__new__(_agent_websocket_server_type())
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", AsyncMock())
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    messages = {"m1": {"status": "normal", "text": "current"}}
    channel.inspect_persistent_group = AsyncMock(return_value={"status": channel_status, "messages": messages})
    manager.register_channel(channel)
    handler._feishu_group_handler = manager.inspect_feishu_group

    async def respond(envelope):
        request = SimpleNamespace(request_id=envelope.request_id, channel_id="feishu", params=envelope.params)
        await server._handle_feishu_group_response(object(), request, asyncio.Lock())

    async def push(message):
        await handler._handle_agent_server_push(build_server_push_wire(message))
        return True

    handler.agent_client = SimpleNamespace(send_request=respond)
    server.send_push = push
    result = await server._inspect_feishu_group("cli_bound", "oc_group", message_ids)
    assert result["status"] == expected
    if expected == "ok":
        assert result["messages"] == messages
    assert server._feishu_group_waiters == {}


@pytest.mark.asyncio
async def test_group_channel_checks_live_bot_membership_and_fails_closed():
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(is_in_chat=False))
    channel._api_client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat_members=SimpleNamespace(
        ais_in_chat=AsyncMock(return_value=response)))))
    assert (await channel.inspect_persistent_group("oc_group"))["status"] == "left"
    response.data.is_in_chat = True
    assert (await channel.inspect_persistent_group("oc_group"))["status"] == "active"
    response.data.is_in_chat = None
    assert (await channel.inspect_persistent_group("oc_group"))["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("message_ids,valid_status,wrong_status", [
    ((), "active", "ok"), (("m1",), "ok", "active"),
])
async def test_group_response_must_match_query_kind(monkeypatch, message_ids, valid_status, wrong_status):
    server = object.__new__(_agent_websocket_server_type())
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", AsyncMock())

    async def push(message):
        payload = message["payload"]
        response = SimpleNamespace(request_id="response", channel_id="feishu", params={
            "query_id": payload["query_id"], "app_id": "cli_bound", "chat_id": "oc_group",
            "status": wrong_status, "messages": {},
        })
        await server._handle_feishu_group_response(object(), response, asyncio.Lock())
        assert not server._feishu_group_waiters[payload["query_id"]][0].done()
        response.params["status"] = valid_status
        await server._handle_feishu_group_response(object(), response, asyncio.Lock())
        return True

    server.send_push = push
    assert (await server._inspect_feishu_group("cli_bound", "oc_group", message_ids))["status"] == valid_status
    assert server._feishu_group_waiters == {}


@pytest.mark.asyncio
async def test_group_caches_group_name_but_not_membership(monkeypatch):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    now = [0.0]
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect.time.monotonic",
                        lambda: now[0])
    membership = SimpleNamespace(success=lambda: True, data=SimpleNamespace(is_in_chat=True))
    member_api = AsyncMock(return_value=membership)
    name_api = AsyncMock(return_value=SimpleNamespace(success=lambda: True, data=SimpleNamespace(name="Project")))
    channel._api_client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        chat_members=SimpleNamespace(ais_in_chat=member_api), chat=SimpleNamespace(aget=name_api))))

    assert (await channel.inspect_persistent_group("oc_group"))["group_name"] == "Project"
    assert (await channel.inspect_persistent_group("oc_group"))["group_name"] == "Project"
    assert member_api.await_count == 2
    assert name_api.await_count == 1
    now[0] = 301.0
    assert (await channel.inspect_persistent_group("oc_group"))["group_name"] == "Project"
    assert name_api.await_count == 2
    membership.data.is_in_chat = False
    assert (await channel.inspect_persistent_group("oc_group")) == {"status": "left"}
    assert member_api.await_count == 4


@pytest.mark.asyncio
async def test_group_gateway_refreshes_edited_and_recalled_bodies_in_the_target_group():
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    membership = SimpleNamespace(success=lambda: True, data=SimpleNamespace(is_in_chat=True))
    edited = SimpleNamespace(message_id="m1", chat_id="oc_group", deleted=False, updated=True,
        update_time=int(NOW.timestamp() * 1000), create_time=int(NOW.timestamp() * 1000),
        body=SimpleNamespace(content='{"text":"new body"}'))
    recalled = SimpleNamespace(message_id="m2", chat_id="oc_group", deleted=True)

    async def read(request):
        item = edited if request.message_id == "m1" else recalled
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=[item]))

    channel._api_client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        chat_members=SimpleNamespace(ais_in_chat=AsyncMock(return_value=membership)),
        message=SimpleNamespace(aget=read))))
    result = await channel.inspect_persistent_group("oc_group", ("m1", "m2"))
    assert result["status"] == "ok"
    channel._api_client.im.v1.chat_members.ais_in_chat.assert_not_awaited()
    assert result["messages"]["m1"]["text"] == "new body"
    assert result["messages"]["m2"] == {"status": "recalled"}
    edited.chat_id = "other-group"
    assert (await channel.inspect_persistent_group("oc_group", ("m1",)))["messages"]["m1"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_group_message_read_failure_stays_unknown_instead_of_unreadable():
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())

    async def read(_request):
        return SimpleNamespace(success=lambda: False, code=99991403, msg="quota exceeded", data=None)

    channel._api_client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(aget=read))))

    result = await channel.inspect_persistent_group("oc_group", ("m1",))

    assert result == {"status": "ok", "messages": {"m1": {"status": "unknown"}}}


@pytest.mark.asyncio
async def test_group_platform_lifecycle_event_enters_only_the_bound_persistent_route():
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    events = []

    async def receive(event):
        events.append(event)
        return InboundDisposition.HANDLED

    channel.set_persistent_group_fanout_callback(receive)
    payload = {"header": {"event_id": "recall-1", "create_time": str(int(NOW.timestamp() * 1000))},
               "event": {"chat_id": "oc_group", "message_id": "m1"}}
    await channel._on_persistent_change(payload, "recalled")
    assert events[0].event_kind == "recalled" and events[0].message_id == "m1"
    assert events[0].changed_at == NOW


def _event(**overrides) -> FeishuInboundEvent:
    values = {
        "receiving_app_id": "cli_bound",
        "expected_agent_id": "mate_1",
        "message_id": "om_1",
        "chat_id": "oc_group",
        "chat_type": "group",
        "sender_type": "user",
        "sender_id": "ou_alice",
        "sender_name": "Alice",
        "sender_app_id": None,
        "received_at": NOW,
        "text": "hello",
        "mention_ids": ("ou_mate",),
        "reply_to": "om_parent",
        "attachment_refs": ("feishu:attachment:om_1:file_1",),
    }
    values.update(overrides)
    return FeishuInboundEvent(**values)


def _agent_websocket_server_type():
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    except SyntaxError as exc:
        if "invalid escape sequence" in str(exc):
            pytest.skip("local agent-core dependency emits a SyntaxWarning promoted to SyntaxError")
        raise
    return AgentWebSocketServer


def test_configured_feishu_bindings_use_one_to_one_persistent_agent_id():
    bindings = configured_feishu_bindings(
        {
            "channels": {
                "feishu": {
                    "apps": [
                        {"app_id": "cli_a", "persistent_agent_id": "mate_a"},
                        {"app_id": "cli_personal", "agent_id": "mate_personal"},
                        {"app_id": "cli_legacy", "persistent_agent_id": "mate_legacy"},
                        {"app_id": "cli_old_key", "agent_id": "mate_old_key"},
                        {"app_id": "cli_duplicate_agent", "persistent_agent_id": "mate_a"},
                    ]
                },
                "feishu_enterprise": {
                    "engineering": {"app_id": "cli_b", "persistent_agent_id": "mate_b"},
                },
            }
        }
    )

    assert [(item.app_id, item.agent_id, item.namespace_id) for item in bindings] == [
        ("cli_b", "mate_b", "local"),
        ("cli_legacy", "mate_legacy", "local"),
    ]


def test_production_binding_resolver_uses_owner_config_and_receiver_app_id(monkeypatch):
    from jiuwenswarm.common import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "feishu": {"apps": [{"app_id": "cli_bound", "persistent_agent_id": "mate_1"}]}
            }
        },
    )

    result = resolve_configured_feishu_bindings("feishu", "cli_bound")

    assert len(result) == 1
    assert (result[0].app_id, result[0].agent_id, result[0].namespace_id) == (
        "cli_bound",
        "mate_1",
        "local",
    )


def test_feishu_event_payload_round_trip_preserves_normalized_message_and_binding_hint():
    event = _event(receiver_bot_mentioned=True)

    restored = FeishuInboundEvent.from_payload(event.to_payload())

    assert restored == event
    assert restored.receiver_bot_mentioned is True


@pytest.mark.parametrize(
    ("chat_type", "expected_scope"),
    [
        ("group", frozenset({"chat", "workspace_read"})),
        ("p2p", frozenset({"chat", "workspace_read", "workspace_knowledge_read"})),
    ],
)
def test_feishu_access_snapshot_grants_workspace_read_without_write(chat_type, expected_scope):
    access = resolve_feishu_access(None, _event(chat_type=chat_type, reply_to=None))

    assert access.principal_ref == "feishu:user:ou_alice"
    assert access.startup_scope == expected_scope
    assert access.source_scope == expected_scope
    assert "workspace_write" not in access.startup_scope
    assert "workspace_write" not in access.source_scope
    assert access.policy_revision


@pytest.mark.asyncio
async def test_default_feishu_runtime_uses_one_owner_sqlite_store_and_closes_manager(tmp_path):
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate")

    await manager.start()
    await manager.aclose()

    assert (tmp_path / "jiuwenmate" / "agents.sqlite3").is_file()


@pytest.mark.asyncio
async def test_default_runtime_consumes_message_refresh_without_rechecking_membership(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    lookup = AsyncMock(return_value={"status": "ok", "messages": {
        "m1": {"status": "edited", "text": "updated", "changed_at": NOW.isoformat()},
    }})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="original")
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1")
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == "updated"
        assert manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))[0].text == "updated"
        lookup.assert_awaited_once_with("cli_bound", "oc_group", ("m1",))
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_message_refresh_replaces_a_mention_placeholder_at_the_same_revision(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    lookup = AsyncMock(return_value={"status": "ok", "messages": {
        "m1": {
            "status": "normal",
            "text": "@all 大家都自我介绍下",
            "changed_at": NOW.isoformat(),
            "attachment_refs": (),
        },
    }})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="@_all 大家都自我介绍下")
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1", changed_at=NOW)
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == "@all 大家都自我介绍下"
        stored = manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))[0]
        assert stored.text == "@all 大家都自我介绍下"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_message_refresh_still_rejects_a_different_body_at_the_same_revision(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    lookup = AsyncMock(return_value={"status": "ok", "messages": {
        "m1": {"status": "normal", "text": "changed", "changed_at": NOW.isoformat(), "attachment_refs": ()},
    }})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="original")
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1", changed_at=NOW)
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == "original"
        stored = manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))[0]
        assert stored.text == "original"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_message_refresh_restores_a_rich_text_mention_omitted_from_the_archive(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    stored_text = " 请完成一次客服工单运营分析"
    restored_text = "@ai员工生活小助手 请完成一次客服工单运营分析"
    lookup = AsyncMock(return_value={"status": "ok", "messages": {
        "m1": {
            "status": "normal",
            "text": restored_text,
            "changed_at": NOW.isoformat(),
            "attachment_refs": (),
        },
    }})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text=stored_text)
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1", changed_at=NOW)
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == restored_text
        stored = manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))[0]
        assert stored.text == restored_text
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_message_refresh_without_a_feishu_answer_keeps_the_archived_text(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    lookup = AsyncMock(return_value={"status": "unknown"})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="original")
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1")
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == "original"
        assert refreshed[0].status == "normal"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_inconclusive_message_refresh_keeps_the_archived_trigger(tmp_path):
    from openjiuwen.harness import persistent_agent as core

    lookup = AsyncMock(return_value={"status": "ok", "messages": {"m1": {"status": "unknown"}}})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access("local", "mate_1", group.group_chat_id, "cli_bound",
                                       observed_at=NOW, source_ref="joined:1")
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    message = core.GroupMessage(message_id="m1", sender_id="ou_alice", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="original")
    manager.group_archive.append("local", group.group_chat_id, message, source_ref="event:m1")
    try:
        refreshed = await manager.refresh_group_messages(access, group, (message,))
        assert refreshed[0].text == "original"
        assert refreshed[0].status == "normal"
        stored = manager.group_archive.current_messages("local", group.group_chat_id, ("m1",))[0]
        assert stored.text == "original"
        assert stored.status == "normal"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_inconclusive_membership_keeps_an_active_binding_until_feishu_says_left(tmp_path, monkeypatch):
    binding = FeishuBinding(
        binding_id="binding-cli",
        platform="feishu",
        app_id="cli_bound",
        namespace_id="local",
        agent_id="mate_1",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.jiuwenmate.feishu_group_fanout.resolve_configured_feishu_bindings",
        lambda platform, app_id: (binding,) if app_id == "cli_bound" else (),
    )
    lookup = AsyncMock(return_value={"status": "unknown"})
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate", verify_membership=lookup)
    group = manager.group_chats.resolve("local", "feishu", "oc_group", now=NOW)
    manager.group_chats.observe_access(
        "local", "mate_1", group.group_chat_id, "cli_bound", observed_at=NOW, source_ref="joined:1",
    )
    access = manager.group_chats.access_for("local", "mate_1", group.group_chat_id)
    try:
        assert await manager.verify_group_membership(access, group) == "active"
        lookup.return_value = {"status": "left"}
        assert await manager.verify_group_membership(access, group) == "left"
        assert manager.group_chats.access_for("local", "mate_1", group.group_chat_id).status == "left"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_default_feishu_manager_owns_result_delivery_worker_when_configured(tmp_path):
    async def send_delivery(_job):
        return None

    _fanout, manager = create_default_persistent_group_fanout(
        tmp_path / "jiuwenmate", send_delivery=send_delivery
    )
    service = manager.result_delivery_service

    assert service is not None and service._task is None
    await manager.start()
    assert service._task is not None and not service._task.done()
    await manager.aclose()
    assert service._task is None


@pytest.mark.asyncio
async def test_message_handler_forwards_bound_event_over_e2a_and_maps_disposition():
    client = MagicMock()
    client.send_request = AsyncMock(
        return_value=SimpleNamespace(ok=True, payload={"disposition": "handled"})
    )
    handler = object.__new__(MessageHandler)
    handler.agent_client = client

    result = await handler.handle_persistent_feishu_event(_event())

    assert result is InboundDisposition.HANDLED
    envelope = client.send_request.await_args.args[0]
    assert envelope.method == ReqMethod.JIUWENMATE_FEISHU_INBOUND.value
    assert envelope.channel == "feishu"
    assert envelope.params["event"] == _event().to_payload()

    agent_request = e2a_to_agent_request(envelope)
    assert agent_request.req_method is ReqMethod.JIUWENMATE_FEISHU_INBOUND
    assert agent_request.params["event"]["expected_agent_id"] == "mate_1"


@pytest.mark.asyncio
async def test_unbound_feishu_event_does_not_make_an_e2a_request():
    client = MagicMock()
    client.send_request = AsyncMock()
    handler = object.__new__(MessageHandler)
    handler.agent_client = client

    result = await handler.handle_persistent_feishu_event(_event(expected_agent_id=None))

    assert result is InboundDisposition.UNCLAIMED
    client.send_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_server_dispatches_feishu_request_without_entering_personal_chat_path(monkeypatch):
    AgentWebSocketServer = _agent_websocket_server_type()

    server = object.__new__(AgentWebSocketServer)
    fanout = MagicMock()
    fanout.handle = AsyncMock(return_value=InboundDisposition.HANDLED)
    server._ensure_persistent_group_fanout = AsyncMock(return_value=fanout)
    request = AgentRequest(
        request_id="jm08-1",
        channel_id="feishu",
        req_method=ReqMethod.JIUWENMATE_FEISHU_INBOUND,
        params={"event": _event().to_payload()},
    )
    sends: list[dict] = []

    async def capture_send(_ws, wire):
        sends.append(wire)

    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", capture_send)

    await server.dispatch_parsed_request(object(), request, asyncio.Lock())

    fanout.handle.assert_awaited_once()
    assert len(sends) == 1
    assert sends[0]["body"]["result"]["disposition"] == "handled"


@pytest.mark.asyncio
async def test_agent_server_starts_jm_runtime_only_for_bound_apps_and_closes_it(monkeypatch):
    AgentWebSocketServer = _agent_websocket_server_type()
    import jiuwenswarm.server.jiuwenmate.feishu_group_fanout as module

    fanout = object()
    manager = MagicMock()
    manager.start = AsyncMock()
    manager.recover = AsyncMock()
    manager.aclose = AsyncMock()
    factory = MagicMock(return_value=(fanout, manager))
    monkeypatch.setattr(module, "configured_feishu_bindings", lambda: ())
    monkeypatch.setattr(module, "create_default_persistent_group_fanout", factory)

    server = object.__new__(AgentWebSocketServer)
    server._feishu_runtime_lock = asyncio.Lock()
    server._feishu_group_fanout = None
    server._feishu_agent_manager = None

    assert await server._ensure_persistent_group_fanout() is None
    factory.assert_not_called()

    monkeypatch.setattr(
        module, "configured_feishu_bindings", lambda: (SimpleNamespace(namespace_id="local"),),
    )
    assert await server._ensure_persistent_group_fanout() is fanout
    assert await server._ensure_persistent_group_fanout() is fanout
    manager.start.assert_awaited_once()
    manager.recover.assert_awaited_once_with(
        "local", channels=frozenset({"feishu", "dingtalk"}), include_unrouted=False,
    )
    assert factory.call_args.kwargs["send_delivery"] == server._send_result_delivery

    await server._close_persistent_group_fanout()
    manager.aclose.assert_awaited_once()
    assert server._feishu_group_fanout is None


@pytest.mark.asyncio
async def test_agent_server_routes_result_delivery_ack_to_persistent_manager(monkeypatch):
    AgentWebSocketServer = _agent_websocket_server_type()
    server = object.__new__(AgentWebSocketServer)
    delivery_service = MagicMock()
    delivery_service.apply_ack = AsyncMock(return_value={"status": "delivered"})
    server._feishu_agent_manager = SimpleNamespace(result_delivery_service=delivery_service)
    waiter = asyncio.get_running_loop().create_future()
    server._result_delivery_waiters = {"del_1": waiter}
    request = AgentRequest(
        request_id="ack-1",
        channel_id="feishu",
        req_method=ReqMethod.JIUWENMATE_DELIVERY_ACK,
        params={
            "delivery_id": "del_1",
            "stage": "send",
            "outcome": "delivered",
            "external_message_id": "om_reply",
        },
    )
    sent = []

    async def capture_send(_ws, wire):
        sent.append(wire)

    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", capture_send)

    await server.dispatch_parsed_request(object(), request, asyncio.Lock())

    delivery_service.apply_ack.assert_awaited_once_with(request.params)
    assert waiter.result() == request.params
    assert len(sent) == 1
    assert sent[0]["body"]["result"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_agent_server_delivery_push_waits_for_correlated_gateway_ack():
    AgentWebSocketServer = _agent_websocket_server_type()
    server = object.__new__(AgentWebSocketServer)
    server._result_delivery_waiters = {}
    sent = []

    async def send_push(message):
        sent.append(message)
        waiter = server._result_delivery_waiters["del_1"]
        waiter.set_result(
            {
                "delivery_id": "del_1",
                "stage": "send",
                "outcome": "delivered",
                "external_message_id": "om_sent",
            }
        )
        return True

    server.send_push = send_push
    result = await server._send_result_delivery(
        {
            "delivery_id": "del_1",
            "app_id": "cli_bound",
            "target_id": "oc_group",
            "source_message_id": "om_source",
            "source_reply_to": "om_parent",
            "idempotency_key": "stable-key",
            "text": "final answer",
        }
    )

    assert result["outcome"] == "delivered"
    assert result["external_message_id"] == "om_sent"
    assert sent[0]["payload"]["event_type"] == "jiuwenmate.delivery.send"
    assert sent[0]["payload"]["app_id"] == "cli_bound"
    assert server._result_delivery_waiters == {}


@pytest.mark.asyncio
async def test_agent_server_delivery_push_forwards_handoff_targets():
    AgentWebSocketServer = _agent_websocket_server_type()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._result_delivery_waiters = {}
    sent = []
    mentions = [{"agent_id": "agent-target", "name": "agent学习小助手", "app_ids": ["cli_target"]}]

    async def send_push(message):
        sent.append(message)
        waiter = server._result_delivery_waiters["del_1"]
        waiter.set_result(
            {
                "delivery_id": "del_1",
                "stage": "send",
                "outcome": "delivered",
                "external_message_id": "om_sent",
            }
        )
        return True

    server.send_push = send_push
    await server._send_result_delivery(
        {
            "delivery_id": "del_1",
            "app_id": "cli_bound",
            "target_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "请接手 [[agent:agent-target]]",
            "mention_targets": mentions,
        }
    )

    assert sent[0]["payload"]["mention_targets"] == mentions


@pytest.mark.asyncio
async def test_gateway_consumes_result_push_and_returns_correlated_e2a_ack():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    client = MagicMock()
    client.send_request = AsyncMock(return_value=SimpleNamespace(ok=True, payload={}))
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._result_delivery_handler = AsyncMock(
        return_value={"outcome": "delivered", "external_message_id": "om_sent"}
    )
    payload = {
        "event_type": "jiuwenmate.delivery.send",
        "delivery_id": "del_1",
        "stage": "send",
        "app_id": "cli_bound",
        "chat_id": "oc_group",
        "source_message_id": "om_source",
        "idempotency_key": "stable-key",
        "text": "final answer",
    }

    await handler._handle_agent_server_push(
        build_server_push_wire({"request_id": "jm09_del_1", "channel_id": "feishu", "payload": payload})
    )

    handler._result_delivery_handler.assert_awaited_once_with(payload)
    client.send_request.assert_awaited_once()
    envelope = client.send_request.await_args.args[0]
    assert envelope.method == ReqMethod.JIUWENMATE_DELIVERY_ACK.value
    assert envelope.params == {
        "delivery_id": "del_1",
        "stage": "send",
        "outcome": "delivered",
        "external_message_id": "om_sent",
        "error_code": None,
    }


@pytest.mark.asyncio
async def test_channel_manager_routes_result_delivery_only_to_exact_feishu_app():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    wrong_app = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_other"), MagicMock())
    right_app = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    wrong_app.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    right_app.send_persistent_result = AsyncMock(
        return_value={"outcome": "delivered", "external_message_id": "om_sent"}
    )
    manager.register_channel(wrong_app)
    manager.register_channel(right_app)

    result = await manager.deliver_persistent_result(
        {
            "app_id": "cli_bound",
            "chat_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "final answer",
        }
    )

    right_app.send_persistent_result.assert_awaited_once_with(
        chat_id="oc_group",
        text="final answer",
        source_message_id="om_source",
        idempotency_key="stable-key",
    )
    wrong_app.send_persistent_result.assert_not_awaited()
    assert result["outcome"] == "delivered"


@pytest.mark.asyncio
async def test_channel_manager_maps_handoff_target_to_its_native_bot_identity():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    sender = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_sender", agent_id="agent-sender"), MagicMock()
    )
    target = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_target", agent_id="agent-target", bot_open_id="ou_target"), MagicMock()
    )
    sender.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    target.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    manager.register_channel(sender)
    manager.register_channel(target)

    await manager.deliver_persistent_result(
        {
            "app_id": "cli_sender",
            "chat_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "请接手 [[agent:agent-target]]",
            "mention_targets": [{"agent_id": "agent-target", "name": "接手同事", "app_ids": ["cli_target"]}],
        }
    )

    sender.send_persistent_result.assert_awaited_once_with(
        chat_id="oc_group",
        text="请接手 [[agent:agent-target]]",
        source_message_id="om_source",
        idempotency_key="stable-key",
        mention_targets=[
            {"agent_id": "agent-target", "name": "接手同事", "app_ids": ["cli_target"], "open_id": "ou_target"}
        ],
    )


@pytest.mark.asyncio
async def test_channel_manager_loads_bot_open_id_before_rendering_a_native_mention():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    sender = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_sender", agent_id="agent-sender"), MagicMock()
    )
    target = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_target", agent_id="agent-target", bot_open_id=""), MagicMock()
    )
    sender.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    target.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})

    def remember_open_id() -> None:
        target.config.bot_open_id = "ou_learned"

    target._fetch_bot_open_id = remember_open_id
    manager.register_channel(sender)
    manager.register_channel(target)

    await manager.deliver_persistent_result(
        {
            "app_id": "cli_sender",
            "chat_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "请接手 [[agent:agent-target]]",
            "mention_targets": [{"agent_id": "agent-target", "name": "接手同事", "app_ids": ["cli_target"]}],
        }
    )

    sender.send_persistent_result.assert_awaited_once_with(
        chat_id="oc_group",
        text="请接手 [[agent:agent-target]]",
        source_message_id="om_source",
        idempotency_key="stable-key",
        mention_targets=[
            {"agent_id": "agent-target", "name": "接手同事", "app_ids": ["cli_target"], "open_id": "ou_learned"}
        ],
    )


@pytest.mark.asyncio
async def test_channel_manager_keeps_the_handoff_when_bot_identity_cannot_be_loaded():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    sender = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_sender", agent_id="agent-sender"), MagicMock()
    )
    target = FeishuChannel(
        FeishuConfig(enabled=True, app_id="cli_target", agent_id="agent-target", bot_open_id=""), MagicMock()
    )
    sender.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    target.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})

    def fail_open_id() -> None:
        raise RuntimeError("quota")

    target._fetch_bot_open_id = fail_open_id
    manager.register_channel(sender)
    manager.register_channel(target)

    result = await manager.deliver_persistent_result(
        {
            "app_id": "cli_sender",
            "chat_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "请接手 [[agent:agent-target]]",
            "mention_targets": [{"agent_id": "agent-target", "name": "接手同事", "app_ids": ["cli_target"]}],
        }
    )

    assert result["outcome"] == "delivered"
    sent = sender.send_persistent_result.await_args.kwargs
    assert "open_id" not in sent["mention_targets"][0]


@pytest.mark.asyncio
async def test_channel_manager_sends_direct_message_without_quoting_source():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    manager.register_channel(channel)

    await manager.deliver_persistent_result(
        {
            "app_id": "cli_bound",
            "chat_id": "oc_dm",
            "source_message_id": "om_source",
            "is_direct_message": True,
            "idempotency_key": "stable-key",
            "text": "你好",
        }
    )

    channel.send_persistent_result.assert_awaited_once_with(
        chat_id="oc_dm",
        text="你好",
        source_message_id=None,
        idempotency_key="stable-key",
    )


@pytest.mark.asyncio
async def test_channel_manager_keeps_delivery_pending_while_bound_app_is_disconnected():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)

    result = await manager.deliver_persistent_result(
        {
            "app_id": "cli_bound",
            "chat_id": "oc_group",
            "source_message_id": "om_source",
            "idempotency_key": "stable-key",
            "text": "final answer",
        }
    )

    assert result == {"outcome": "pending", "error_code": "FEISHU_APP_NOT_CONNECTED"}


@pytest.mark.asyncio
async def test_gateway_reports_pending_when_no_delivery_handler_is_available():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    client = MagicMock()
    client.send_request = AsyncMock(return_value=SimpleNamespace(ok=True, payload={}))
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._result_delivery_handler = None

    await handler._handle_agent_server_push(
        build_server_push_wire(
            {
                "request_id": "jm09_del_1",
                "channel_id": "feishu",
                "payload": {
                    "event_type": "jiuwenmate.delivery.send",
                    "delivery_id": "del_1",
                    "stage": "send",
                    "app_id": "cli_bound",
                    "chat_id": "oc_group",
                    "idempotency_key": "stable-key",
                    "text": "final answer",
                },
            }
        )
    )

    client.send_request.assert_awaited_once()
    envelope = client.send_request.await_args.args[0]
    assert envelope.method == ReqMethod.JIUWENMATE_DELIVERY_ACK.value
    assert envelope.params["outcome"] == "pending"
    assert envelope.params["error_code"] == "CHANNEL_ROUTE_UNAVAILABLE"
