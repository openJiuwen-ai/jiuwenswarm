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
    FeishuInboundEvent,
    InboundDisposition,
    configured_feishu_bindings,
    create_default_persistent_group_fanout,
    resolve_configured_feishu_bindings,
    resolve_feishu_access,
)


NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_jm11_gateway_membership_lookup_uses_exact_bound_app():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound", agent_id="mate_1"), MagicMock())
    channel.inspect_persistent_group = AsyncMock(return_value={"status": "active", "group_name": "Project"})
    manager.register_channel(channel)
    result = await manager.inspect_jm11_group({"app_id": "cli_bound", "chat_id": "oc_group"})
    assert result == {"status": "active", "group_name": "Project"}
    missing = await manager.inspect_jm11_group({"app_id": "other", "chat_id": "oc_group"})
    assert missing["status"] == "unknown"


@pytest.mark.asyncio
async def test_jm11_membership_push_returns_a_correlated_response():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    handler = object.__new__(MessageHandler)
    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True)))
    handler._jm11_group_handler = AsyncMock(return_value={"status": "left"})
    payload = {"event_type": "jiuwenmate.group.inspect", "query_id": "query-1",
               "app_id": "cli_bound", "chat_id": "oc_group"}
    await handler._handle_agent_server_push(build_server_push_wire({
        "request_id": "query-1", "channel_id": "feishu", "payload": payload,
    }))
    envelope = handler.agent_client.send_request.await_args.args[0]
    assert envelope.method == "jiuwenmate.group.response"
    assert envelope.params["query_id"] == "query-1" and envelope.params["status"] == "left"


@pytest.mark.asyncio
async def test_jm11_server_rejects_wrong_app_response_and_cleans_up_query(monkeypatch):
    server = object.__new__(_agent_websocket_server_type())
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", AsyncMock())

    async def push(message):
        params = message["payload"]
        query_id = params["query_id"]
        response = SimpleNamespace(request_id="response", channel_id="feishu",
            params={"query_id": query_id, "app_id": "wrong", "chat_id": "oc_group", "status": "active"})
        await server._handle_jm11_group_response(object(), response, asyncio.Lock())
        assert not server._jm11_group_waiters[query_id][0].done()
        response.params["app_id"] = "cli_bound"
        await server._handle_jm11_group_response(object(), response, asyncio.Lock())
        return True

    server.send_push = push
    assert (await server._inspect_jm11_group("cli_bound", "oc_group"))["status"] == "active"
    assert server._jm11_group_waiters == {}
    server.send_push = AsyncMock(return_value=False)
    assert (await server._inspect_jm11_group("cli_bound", "oc_group"))["status"] == "unknown"
    assert server._jm11_group_waiters == {}


@pytest.mark.asyncio
async def test_jm11_channel_checks_live_bot_membership_and_fails_closed():
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
async def test_jm11_gateway_refreshes_edited_and_recalled_bodies_in_the_target_group():
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
    assert result["messages"]["m1"]["text"] == "new body"
    assert result["messages"]["m2"] == {"status": "recalled"}
    edited.chat_id = "other-group"
    assert (await channel.inspect_persistent_group("oc_group", ("m1",)))["messages"]["m1"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_jm11_platform_lifecycle_event_enters_only_the_bound_persistent_route():
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
    event = _event()

    restored = FeishuInboundEvent.from_payload(event.to_payload())

    assert restored == event


def test_feishu_access_snapshot_identifies_sender_without_workspace_scopes():
    access = resolve_feishu_access(None, _event())

    assert access.principal_ref == "feishu:user:ou_alice"
    assert access.startup_scope == frozenset({"chat"})
    assert access.source_scope == frozenset({"chat"})
    assert access.policy_revision


@pytest.mark.asyncio
async def test_default_jm08_runtime_uses_one_owner_sqlite_store_and_closes_manager(tmp_path):
    _fanout, manager = create_default_persistent_group_fanout(tmp_path / "jiuwenmate")

    await manager.start()
    await manager.aclose()

    assert (tmp_path / "jiuwenmate" / "agents.sqlite3").is_file()


@pytest.mark.asyncio
async def test_default_jm08_manager_owns_jm09_delivery_worker_when_configured(tmp_path):
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
async def test_agent_server_dispatches_jm08_request_without_entering_personal_chat_path(monkeypatch):
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
    manager.aclose = AsyncMock()
    factory = MagicMock(return_value=(fanout, manager))
    monkeypatch.setattr(module, "configured_feishu_bindings", lambda: ())
    monkeypatch.setattr(module, "create_default_persistent_group_fanout", factory)

    server = object.__new__(AgentWebSocketServer)
    server._jm08_runtime_lock = asyncio.Lock()
    server._jm08_fanout = None
    server._jm08_agent_manager = None

    assert await server._ensure_persistent_group_fanout() is None
    factory.assert_not_called()

    monkeypatch.setattr(module, "configured_feishu_bindings", lambda: (object(),))
    assert await server._ensure_persistent_group_fanout() is fanout
    assert await server._ensure_persistent_group_fanout() is fanout
    manager.start.assert_awaited_once()
    assert factory.call_args.kwargs["send_delivery"] == server._send_jm09_delivery

    await server._close_persistent_group_fanout()
    manager.aclose.assert_awaited_once()
    assert server._jm08_fanout is None


@pytest.mark.asyncio
async def test_agent_server_routes_jm09_delivery_ack_to_persistent_manager(monkeypatch):
    AgentWebSocketServer = _agent_websocket_server_type()
    server = object.__new__(AgentWebSocketServer)
    delivery_service = MagicMock()
    delivery_service.apply_ack = AsyncMock(return_value={"status": "delivered"})
    server._jm08_agent_manager = SimpleNamespace(result_delivery_service=delivery_service)
    waiter = asyncio.get_running_loop().create_future()
    server._jm09_delivery_waiters = {"del_1": waiter}
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
    server._jm09_delivery_waiters = {}
    sent = []

    async def send_push(message):
        sent.append(message)
        waiter = server._jm09_delivery_waiters["del_1"]
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
    result = await server._send_jm09_delivery(
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
    assert server._jm09_delivery_waiters == {}


@pytest.mark.asyncio
async def test_gateway_consumes_jm09_push_and_returns_correlated_e2a_ack():
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    client = MagicMock()
    client.send_request = AsyncMock(return_value=SimpleNamespace(ok=True, payload={}))
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._jm09_delivery_handler = AsyncMock(
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

    handler._jm09_delivery_handler.assert_awaited_once_with(payload)
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
async def test_channel_manager_routes_jm09_delivery_only_to_exact_feishu_app():
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

    result = await manager.deliver_jm09_persistent_result(
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
        mention_ids=(),
    )
    wrong_app.send_persistent_result.assert_not_awaited()
    assert result["outcome"] == "delivered"


@pytest.mark.asyncio
async def test_channel_manager_sends_direct_message_without_quoting_source():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel.send_persistent_result = AsyncMock(return_value={"outcome": "delivered"})
    manager.register_channel(channel)

    await manager.deliver_jm09_persistent_result(
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
        mention_ids=(),
    )


@pytest.mark.asyncio
async def test_channel_manager_keeps_delivery_pending_while_bound_app_is_disconnected():
    handler = object.__new__(MessageHandler)
    manager = ChannelManager(handler)

    result = await manager.deliver_jm09_persistent_result(
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
    handler._jm09_delivery_handler = None

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
