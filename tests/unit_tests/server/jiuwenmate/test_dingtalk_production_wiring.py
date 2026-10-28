"""DingTalk JM wiring with real SQLite/scheduler/commit/outbox and substituted PA/network."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_connect import DingTalkChannel, DingTalkConfig
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.gateway.routing.keys import ChannelKey
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkInboundEvent, DingTalkInboundHandler
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import (
    FeishuBinding,
    FeishuInboundEvent,
    InboundDisposition,
    PersistentGroupFanout,
)
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import ConnectionRecord
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WorkspaceStore
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import FakeCore, FakeService, settle, stub_invocation_builder
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel, FakeChannelManager
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
DINGTALK_APP = {"client_id": "app-dingtalk", "client_secret": "fake-secret", "persistent_agent_id": "agent-dingtalk",
                "robot_code": "robot-test"}


def _event(**changes):
    return DingTalkInboundEvent(
        receiving_app_id="app-dingtalk", expected_agent_id="agent-dingtalk", message_id="message-test",
        conversation_id="conversation-test", sender_staff_id="staff-test", sender_name="Test sender",
        text="test task", robot_code="robot-test", received_at=NOW, **changes,
    )


def _dingtalk_channel(**config):
    channel = DingTalkChannel(DingTalkConfig(api_base="https://example.invalid", **config), MagicMock(spec=RobotMessageRouter))
    channel._access_token = "fake-token"
    channel._token_expiry = time.time() + 3600
    return channel


def _connection(agent_id, channel, app_id, config_id="colleague-test"):
    return ConnectionRecord(namespace_id="local", agent_id=agent_id, config_id=config_id, channel=channel,
                            connection_key=app_id, created_at=NOW)


@pytest_asyncio.fixture
async def host(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    config = {"channels": {
        "dingtalk": {"apps": [dict(DINGTALK_APP)]},
        "feishu": {"apps": [{"app_id": "app-feishu", "persistent_agent_id": "agent-feishu"}]},
    }}
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    stub_invocation_builder(monkeypatch)
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    workspaces = WorkspaceStore(tmp_path / "workspaces")
    service = FakeService(FakeCore(), workspaces)
    service.read_settings = lambda namespace_id, agent_id: SimpleNamespace(group_reply_mode="adaptive")
    connections = {"agent-dingtalk": _connection("agent-dingtalk", "dingtalk", "app-dingtalk")}
    service.connection_of = lambda namespace_id, agent_id: connections.get(agent_id)
    contexts, sent = [], []
    channel = _dingtalk_channel(
        client_id="app-dingtalk", robot_code="robot-test", agent_id="agent-dingtalk", is_default=False,
    )

    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"processQueryKey": "platform-send-test"})

    channel._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    gateway = ChannelManager(SimpleNamespace(), config={"dingtalk": config["channels"]["dingtalk"]})
    gateway.register_channel(channel)

    async def send(job):
        receipt = await gateway.deliver_persistent_result({**job, "chat_id": job["target_id"]})
        return {"delivery_id": job["delivery_id"], "stage": "send", **receipt}

    deliveries = ResultDeliveryService(
        repository, namespace_id="local", channels=frozenset({"feishu", "dingtalk"}), send_delivery=send,
    )

    async def execute(spec, context, inputs):
        contexts.append(context)
        output = core.AgentEvent.create(context.execution, event_type=core.EventType.OUTPUT_PROPOSED, local_seq=1,
            occurred_at=NOW, visibility="candidate", payload={"output_id": "out-test", "target_ref": inputs.reply_target,
                                                              "share_mode": "message", "text": "test result"})
        final = core.AgentEvent.create(context.execution, event_type=core.EventType.EXECUTION_FINAL, local_seq=2,
            occurred_at=NOW, payload={"status": "completed", "output_refs": ("out-test",)})
        for event in (output, final):
            await context.ports.commit.record(context.execution, event)
            yield event

    manager = AgentManager(service, repository, WindowBackendStore(tmp_path / "agents.sqlite3"),
        BudgetRepository(tmp_path / "agents.sqlite3"), execute=execute, delivery_service=deliveries, clock=lambda: NOW)
    async def verify_group_membership(access, group):
        return "active" if group.platform == "dingtalk" and access.status == "active" else "unknown"

    manager.verify_group_membership = verify_group_membership
    inbound = DingTalkInboundHandler(service=service, sessions=repository, agent_manager=manager, config_loader=lambda: config)
    await manager.start()
    yield SimpleNamespace(config=config, repository=repository, service=service, manager=manager, inbound=inbound,
        contexts=contexts, sent=sent, gateway=gateway, deliveries=deliveries, workspaces=workspaces, channel=channel,
        connections=connections, db=tmp_path / "agents.sqlite3")
    # Delivery can be observed just before the substituted execution stream ends.
    await settle(lambda: not manager.scheduler.running)
    await manager.aclose()
    await channel._http.aclose()


def _requests(host, app_id="app-dingtalk"):
    conversation = ConversationRef.im_direct("dingtalk", json.dumps([app_id, "conversation-test"], separators=(",", ":")))
    session = host.repository.find_session("local", "agent-dingtalk", conversation)
    return host.repository.session_requests("local", session.session_id) if session else []


def _delivered(host):
    rows = _requests(host)
    deliveries = host.repository.result_deliveries("local", rows[0].record.request_id) if rows else []
    return bool(deliveries) and deliveries[0]["status"] == "delivered"


def _group_requests(host, conversation_id="conversation-test"):
    group = host.manager.group_chats.resolve("local", "dingtalk", conversation_id, now=NOW)
    session = host.repository.find_session("local", "agent-dingtalk", ConversationRef.group(group.group_chat_id))
    return host.repository.session_requests("local", session.session_id) if session else []


def _group_delivered(host):
    rows = _group_requests(host)
    deliveries = host.repository.result_deliveries("local", rows[0].record.request_id) if rows else []
    return bool(deliveries) and deliveries[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_redelivery_reuses_one_request_execution_and_outbox_send(host):
    assert await host.inbound.handle(_event()) is InboundDisposition.HANDLED
    await settle(lambda: _delivered(host))
    assert await host.inbound.handle(_event()) is InboundDisposition.HANDLED
    rows = _requests(host)
    assert len(rows) == len(host.contexts) == len(host.sent) == 1
    record = rows[0].record
    assert record.route_snapshot.channel == "dingtalk" and record.route_snapshot.app_id == "app-dingtalk"
    assert record.route_snapshot.target_id == "staff-test"
    assert record.request_input.source.message_id == "message-test"
    assert record.request_input.reply_target == "staff-test"
    assert host.sent[0]["robotCode"] == "robot-test"
    delivery = host.repository.result_deliveries("local", record.request_id)[0]
    assert delivery["external_message_id"] == "platform-send-test"
    foreign = host.workspaces.ensure("local", "agent-feishu") / "output" / "foreign.txt"
    foreign.write_text("test other-channel output")
    scope = await host.contexts[0].ports.workspace_access.resolve_read_scope(host.contexts[0].execution)
    assert scope.workspace_root == host.workspaces.path("local", "agent-dingtalk")
    assert not foreign.is_relative_to(scope.workspace_root)


@pytest.mark.asyncio
async def test_mentioned_group_message_runs_bound_agent_and_replies_to_that_group(host):
    callback = {
        "conversationType": "2", "conversationId": "conversation-test", "conversationTitle": "Test group",
        "msgtype": "text", "msgId": "group-message-test", "senderStaffId": "staff-test",
        "senderNick": "Test sender", "robotCode": "robot-test", "senderCorpId": "corp-test",
        "chatbotCorpId": "corp-test", "isInAtList": True, "chatbotUserId": "bot-test",
        "atUsers": [{"dingtalkId": "bot-test"}], "text": {"content": "please help"},
    }
    event = DingTalkInboundEvent.from_callback(callback, app_id="app-dingtalk", agent_id="agent-dingtalk")

    assert await host.inbound.handle(event) is InboundDisposition.HANDLED
    await settle(lambda: _group_delivered(host))

    rows = _group_requests(host)
    assert len(rows) == 1
    record = rows[0].record
    assert record.request_input.kind == "group_message"
    assert record.request_input.source.mention_ids == ("bot-test", "agent-dingtalk")
    assert record.request_input.reply_target == "conversation-test"
    assert record.route_snapshot.channel == "dingtalk" and record.route_snapshot.app_id == "app-dingtalk"
    assert record.route_snapshot.target_id == "conversation-test"
    assert len(host.sent) == 1
    assert host.sent[0]["openConversationId"] == "conversation-test"


@pytest.mark.asyncio
async def test_dingtalk_group_waiting_notice_uses_the_group_send_api(host):
    result = await host.gateway.deliver_thinking_notice({
        "channel": "dingtalk",
        "app_id": "app-dingtalk",
        "chat_id": "conversation-test",
        "is_direct_message": False,
        "source_message_id": "group-message-test",
        "text": "I am working on it.",
        "idempotency_key": "thinking:group-test",
    })

    assert result["outcome"] == "delivered"
    assert host.sent[-1]["openConversationId"] == "conversation-test"


@pytest.mark.asyncio
async def test_group_redelivery_reuses_the_archived_message_without_a_second_execution(host):
    event = replace(_event(
        conversation_type="2",
        receiver_bot_mentioned=True,
        mention_ids=("bot-test",),
    ), message_id="group-redelivery")

    assert await host.inbound.handle(event) is InboundDisposition.HANDLED
    await settle(lambda: _group_delivered(host))
    assert await host.inbound.handle(event) is InboundDisposition.HANDLED

    rows = _group_requests(host)
    assert len(rows) == len(host.contexts) == len(host.sent) == 1


@pytest.mark.asyncio
async def test_unmentioned_adaptive_group_message_runs_the_bound_agent(host):
    event = replace(_event(
        conversation_type="2",
        receiver_bot_mentioned=False,
        mention_ids=(),
    ), message_id="adaptive-group-message")

    assert await host.inbound.handle(event) is InboundDisposition.HANDLED
    await settle(lambda: _group_delivered(host))

    rows = _group_requests(host)
    assert len(rows) == 1
    assert rows[0].record.request_input.kind == "group_message"
    assert rows[0].record.request_input.source.mention_ids == ()
    assert rows[0].record.route_snapshot.target_id == "conversation-test"


@pytest.mark.asyncio
async def test_passive_group_message_is_archived_without_creating_a_request(host):
    host.service.read_settings = lambda namespace_id, agent_id: SimpleNamespace(group_reply_mode="passive")
    event = replace(_event(
        conversation_type="2",
        receiver_bot_mentioned=False,
        mention_ids=(),
    ), message_id="passive-group-message")

    assert await host.inbound.handle(event) is InboundDisposition.HANDLED

    group = host.manager.group_chats.resolve("local", "dingtalk", "conversation-test", now=NOW)
    archived = host.manager.group_archive.current_messages("local", group.group_chat_id, (event.message_id,))
    assert archived[0].text == "test task"
    assert not _group_requests(host)


@pytest.mark.asyncio
async def test_default_runtime_accepts_an_active_bound_dingtalk_group_observation(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import create_default_persistent_group_fanout

    config = {"channels": {"dingtalk": {"apps": [dict(DINGTALK_APP)]}}}
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    _fanout, manager = create_default_persistent_group_fanout(
        tmp_path / "jiuwenmate", verify_membership=AsyncMock(),
    )
    try:
        group = manager.group_chats.resolve("local", "dingtalk", "conversation-test", now=NOW)
        manager.group_chats.observe_access(
            "local", "agent-dingtalk", group.group_chat_id, "app-dingtalk", observed_at=NOW,
            source_ref="dingtalk:message:group-message",
        )
        access = manager.group_chats.access_for("local", "agent-dingtalk", group.group_chat_id)

        assert await manager.verify_group_membership(access, group) == "active"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_disabled_or_mismatched_agent_is_not_executed(host):
    host.service.disabled_agents.add("agent-dingtalk")
    assert await host.inbound.handle(_event()) is InboundDisposition.HANDLED
    assert not _requests(host)
    assert await host.inbound.handle(replace(_event(), expected_agent_id="other-agent")) is InboundDisposition.REJECTED
    assert not host.contexts


@pytest.mark.asyncio
async def test_staff_from_another_organization_is_rejected(host):
    event = replace(_event(), sender_corp_id="corp-other", chatbot_corp_id="corp-robot")
    assert await host.inbound.handle(event) is InboundDisposition.REJECTED
    same = replace(_event(), sender_corp_id="corp-robot", chatbot_corp_id="corp-robot")
    assert await host.inbound.handle(same) is InboundDisposition.HANDLED


@pytest.mark.asyncio
async def test_feishu_rejects_a_hand_written_binding_to_the_dingtalk_agent(host):
    # The Feishu entry of the owner's configuration names the agent created for the DingTalk app.
    host.config["channels"]["feishu"]["apps"][0]["persistent_agent_id"] = "agent-dingtalk"
    fanout = PersistentGroupFanout(
        service=host.service, groups=GroupChatRepository(host.db), sessions=host.repository,
        agent_manager=host.manager,
        resolve_bindings=lambda platform, app_id: (FeishuBinding(
            binding_id="feishu:app-feishu", platform="feishu", app_id="app-feishu", namespace_id="local",
            agent_id="agent-dingtalk",
        ),),
        resolve_access=lambda binding, event: None,
    )
    event = FeishuInboundEvent(
        receiving_app_id="app-feishu", message_id="om-test", chat_id="oc-test", chat_type="p2p", sender_type="user",
        sender_id="ou-test", sender_name="Test sender", sender_app_id=None, received_at=NOW, text="test task",
    )
    assert await fanout.handle(event) is InboundDisposition.REJECTED
    assert await host.inbound.handle(_event()) is InboundDisposition.HANDLED


@pytest.mark.parametrize("connection", [
    None,
    _connection("agent-dingtalk", "dingtalk", "other-dingtalk-app"),
    _connection("agent-dingtalk", "feishu", "app-dingtalk"),
])
@pytest.mark.asyncio
async def test_an_agent_not_created_for_this_app_is_rejected(host, connection):
    host.connections["agent-dingtalk"] = connection
    assert await host.inbound.handle(_event()) is InboundDisposition.REJECTED
    assert not host.contexts and not _requests(host)


@pytest.mark.asyncio
async def test_missing_original_app_never_falls_back_and_a_removed_app_is_not_retried(host):
    connecting = {"channel": "dingtalk", "app_id": "app-dingtalk", "chat_id": "staff-test", "text": "test",
                  "idempotency_key": "delivery-test"}
    host.gateway.pop_channels_by_id("dingtalk")
    other = SimpleNamespace(channel_id="feishu", app_id="app-dingtalk", send_persistent_result=AsyncMock())
    host.gateway._channels[ChannelKey("feishu", "app-dingtalk")] = other
    assert (await host.gateway.deliver_persistent_result(connecting))["outcome"] == "pending"
    await host.gateway.set_conf("dingtalk", {"apps": []})
    assert await host.gateway.deliver_persistent_result(connecting) == {
        "outcome": "failed", "error_code": "DINGTALK_APP_NOT_CONFIGURED",
    }
    other.send_persistent_result.assert_not_awaited()
    assert not host.sent


@pytest.mark.asyncio
async def test_feishu_and_enterprise_feishu_keep_their_app_matching(host):
    enterprise = SimpleNamespace(
        channel_id="feishu_enterprise:cli-b", send_persistent_result=AsyncMock(return_value={"outcome": "delivered"}),
        inspect_persistent_group=AsyncMock(return_value={"status": "active"}),
    )
    host.gateway._channels[ChannelKey("feishu_enterprise:cli-b", "cli-b")] = enterprise
    payload = {"channel": "feishu", "app_id": "cli-b", "chat_id": "oc-test", "text": "test", "idempotency_key": "key"}
    assert (await host.gateway.deliver_persistent_result(payload))["outcome"] == "delivered"
    assert (await host.gateway.inspect_feishu_group({"app_id": "cli-b", "chat_id": "oc-test"}))["status"] == "active"
    # A Feishu route never reaches a DingTalk robot that happens to share the application id.
    result = await host.gateway.deliver_persistent_result({**payload, "app_id": "app-dingtalk"})
    assert result == {"outcome": "pending", "error_code": "FEISHU_APP_NOT_CONNECTED"}
    assert not host.sent


async def _dispatch_once(gateway, message):
    queue = [message]

    async def consume(timeout):
        if queue:
            return queue.pop()
        gateway._running = False
        return None

    gateway._message_handler = SimpleNamespace(consume_robot_messages=consume, resolve_app_id=MessageHandler.resolve_app_id)
    gateway._running = True
    await gateway._dispatch_robot_messages()


def _cron_push():
    return Message(id="cron-push-test", type="event", channel_id="dingtalk", session_id="cron-session", params={},
                   timestamp=0.0, ok=True, payload={"event_type": "chat.final", "content": "test reminder"})


@pytest.mark.asyncio
async def test_messages_without_an_app_identity_never_pick_an_arbitrary_robot():
    gateway = ChannelManager(SimpleNamespace())
    jm = _dingtalk_channel(client_id="jm", agent_id="agent-a", is_default=False)
    jm.send = AsyncMock()
    gateway.register_channel(jm)
    await _dispatch_once(gateway, _cron_push())
    jm.send.assert_not_awaited()
    converted = _dingtalk_channel(client_id="converted", agent_id="agent-b", is_default=True)
    converted.send = AsyncMock()
    gateway.register_channel(converted)
    await _dispatch_once(gateway, _cron_push())
    converted.send.assert_awaited_once()
    jm.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_e2a_uses_dingtalk_envelope_and_preserves_event(host):
    handler = object.__new__(MessageHandler)
    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload={"disposition": "handled"})))
    assert await handler.handle_persistent_dingtalk_event(_event()) is InboundDisposition.HANDLED
    envelope = handler.agent_client.send_request.await_args.args[0]
    assert envelope.channel == "dingtalk"
    assert envelope.method == ReqMethod.JIUWENMATE_DINGTALK_INBOUND.value
    assert envelope.params["event"]["message_id"] == "message-test"


@pytest.mark.asyncio
async def test_only_dingtalk_binding_starts_the_existing_im_runtime(monkeypatch):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.jiuwenmate import feishu_group_fanout as fanout_module

    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {"dingtalk": {
        "client_id": "app-dingtalk", "client_secret": "fake-secret", "persistent_agent_id": "agent-dingtalk",
    }}})
    manager = SimpleNamespace(start=AsyncMock(), recover=AsyncMock(), aclose=AsyncMock())
    fanout = SimpleNamespace()
    factory = MagicMock(return_value=(fanout, manager))
    monkeypatch.setattr(fanout_module, "create_default_persistent_group_fanout", factory)
    server = object.__new__(server_module.AgentWebSocketServer)
    assert await server._ensure_persistent_group_fanout() is fanout
    assert await server._ensure_persistent_group_fanout() is fanout
    factory.assert_called_once()
    manager.recover.assert_awaited_once_with("local", channels=frozenset({"feishu", "dingtalk"}), include_unrouted=False)
    await server._close_persistent_group_fanout()
    manager.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_dispatches_dingtalk_without_personal_chat(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.models import E2AEnvelope

    inbound = SimpleNamespace(handle=AsyncMock(return_value=InboundDisposition.HANDLED))
    server = object.__new__(module.AgentWebSocketServer)
    server._ensure_persistent_group_fanout = AsyncMock(return_value=SimpleNamespace(dingtalk_inbound=inbound))
    sender = AsyncMock()
    monkeypatch.setattr(module, "send_wire_payload", sender)
    request = e2a_to_agent_request(E2AEnvelope(request_id="test-transport", channel="dingtalk",
        method=ReqMethod.JIUWENMATE_DINGTALK_INBOUND.value, params={"event": _event().to_payload()}))
    await server.dispatch_parsed_request(object(), request, asyncio.Lock())
    assert inbound.handle.await_args.args[0] == _event()
    sender.assert_awaited_once()


class _AgentServer:
    """Answers Gateway's connection requests: the agent of app X is ``agent-X``.

    ``connections`` is what a colleague-connections query returns; ``refuse`` is an error code
    to answer with; ``running=False`` behaves like an unreachable server.
    """

    def __init__(self):
        self.envelopes = []
        self.gate = None
        self.connections = []
        self.refuse = None
        self.running = True

    async def send_request(self, envelope):
        self.envelopes.append(envelope)
        gate, self.gate = self.gate, None
        if gate is not None:
            await gate.wait()
        if not self.running:
            raise ConnectionError("test AgentServer is not running")
        if self.refuse is not None:
            return SimpleNamespace(ok=False, payload={"error": "refused by test", "code": self.refuse})
        if envelope.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value:
            return SimpleNamespace(ok=True, payload={"agent_id": f"agent-{envelope.params['connection_key']}"})
        if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value:
            return SimpleNamespace(ok=True, payload={"connections": self.connections})
        return SimpleNamespace(ok=True, payload={"removed": True})


@pytest.fixture
def web(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    channel, cm, server = FakeWebChannel(), FakeChannelManager(), _AgentServer()
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm, agent_client=server))

    def saved():
        return config_module.load_yaml_round_trip(path)["channels"]["dingtalk"]

    def seed(dingtalk):
        cm.configs["dingtalk"] = dingtalk
        config_module.dump_yaml_round_trip(path, {"channels": {"dingtalk": dingtalk}})

    return SimpleNamespace(channel=channel, cm=cm, server=server, saved=saved, seed=seed)


async def _call(web, method, params):
    await web.channel.methods[method](object(), f"{method}-test", params, "test-session")
    return web.channel.responses[-1]


@pytest.mark.asyncio
async def test_binding_moves_the_legacy_app_into_the_list_and_settings_edits_keep_it(web):
    web.seed({"enabled": True, "client_id": "ordinary", "client_secret": "fake-old", "api_base": "https://example.invalid",
              "last_sender_id": "staff-old"})
    bound = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-dingtalk", "client_secret": "fake-secret", "config_id": "colleague-a",
    })
    assert bound["ok"] is True
    attach = web.server.envelopes[-1]
    assert attach.method == ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value
    assert attach.params == {"config_id": "colleague-a", "channel": "dingtalk", "connection_key": "app-dingtalk"}
    saved = web.saved()
    assert "client_id" not in saved and saved["last_sender_id"] == "staff-old"
    assert [app["client_id"] for app in saved["apps"]] == ["ordinary", "app-dingtalk"]
    assert saved["apps"][1]["persistent_agent_id"] == "agent-app-dingtalk"
    edited = await _call(web, "channel.dingtalk.set_conf", {"apps": [
        {"client_id": "ordinary", "allow_from": ["staff-test"], "enabled": True},
        {"client_id": "app-dingtalk", "enabled": False},
    ]})
    assert edited["ok"] is True
    ordinary, jm = web.saved()["apps"]
    assert ordinary["allow_from"] == ["staff-test"] and ordinary["client_secret"] == "fake-old"
    assert jm["persistent_agent_id"] == "agent-app-dingtalk" and jm["client_secret"] == "fake-secret"
    assert jm["enabled"] is False
    assert web.cm.configs["dingtalk"] == dict(web.saved())
    listing = await _call(web, "channel.dingtalk.get_conf", {})
    assert [app["client_id"] for app in listing["payload"]["config"]["apps"]] == ["ordinary", "app-dingtalk"]


@pytest.mark.asyncio
async def test_deleting_one_app_keeps_the_others_and_another_app_adds_a_connection(web):
    web.seed({"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "jm-a", "client_secret": "fake", "persistent_agent_id": "agent-jm-a"},
        {"client_id": "jm-b", "client_secret": "fake", "persistent_agent_id": "agent-jm-b"},
    ], "last_sender_id": "staff-old"})
    assert (await _call(web, "channel.dingtalk.set_conf", {"apps": [{"client_id": "jm-a"}, {"client_id": "jm-b"}]}))["ok"]
    saved = web.saved()
    assert [app["client_id"] for app in saved["apps"]] == ["jm-a", "jm-b"] and "last_sender_id" not in saved
    # Another app for the same colleague is another connection with its own agent; jm-a stays.
    added = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "jm-c", "client_secret": "fake", "config_id": "colleague-a",
    })
    assert added["ok"] is True
    assert [(app["client_id"], app["persistent_agent_id"]) for app in web.saved()["apps"]] == [
        ("jm-a", "agent-jm-a"), ("jm-b", "agent-jm-b"), ("jm-c", "agent-jm-c"),
    ]


@pytest.mark.asyncio
async def test_settings_refuse_a_second_ordinary_app(web):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake"}]})
    second = await _call(web, "channel.dingtalk.set_conf", {"apps": [
        {"client_id": "ordinary"}, {"client_id": "two", "allow_from": []},
    ]})
    assert second["ok"] is False and "only one ordinary" in second["error"]
    assert [app["client_id"] for app in web.saved()["apps"]] == ["ordinary"]


@pytest.mark.asyncio
async def test_concurrent_bindings_never_overwrite_each_other(web):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake"}]})
    gate = asyncio.Event()
    web.server.gate = gate
    first = asyncio.create_task(web.channel.methods["channel.dingtalk.bind_agent"](object(), "bind-a", {
        "client_id": "app-a", "client_secret": "fake", "config_id": "colleague-a",
    }, "test-session"))
    # Binding A waits for AgentServer's answer while binding B completes.
    await settle(lambda: len(web.server.envelopes) == 1)
    second = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-b", "client_secret": "fake", "config_id": "colleague-b",
    })
    assert second["ok"] is True and not first.done()
    gate.set()
    await first
    assert [response["ok"] for response in web.channel.responses] == [True, True]
    assert [(app["client_id"], app["persistent_agent_id"]) for app in web.saved()["apps"]] == [
        ("ordinary", ""), ("app-b", "agent-app-b"), ("app-a", "agent-app-a"),
    ]
    assert web.cm.configs["dingtalk"]["apps"] == web.saved()["apps"]


@pytest.mark.asyncio
async def test_a_malformed_allowlist_survives_binding_and_blocks_a_client_id_change(web):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake", "allow_from": "staff-only"}]})
    bound = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-a", "client_secret": "fake", "config_id": "colleague-a",
    })
    assert bound["ok"] is True
    ordinary, jm = web.saved()["apps"]
    assert ordinary["allow_from"] == "staff-only" and jm["allow_from"] == []
    # The settings page omits a malformed allowlist; under another client_id it would be an unrestricted new app.
    renamed = await _call(web, "channel.dingtalk.set_conf", {"apps": [
        {"client_id": "ordinary-new", "client_secret": "fake"}, {"client_id": "app-a"},
    ]})
    assert renamed["ok"] is False and renamed["code"] == "BAD_REQUEST"
    assert [app["client_id"] for app in web.saved()["apps"]] == ["ordinary", "app-a"]


@pytest.mark.asyncio
async def test_overlapping_restarts_leave_every_saved_app_registered():
    from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.lifecycle import DingTalkChannelSet

    apps = [{"client_id": client_id, "client_secret": "fake", "persistent_agent_id": agent_id}
            for client_id, agent_id in (("ordinary", ""), ("app-a", "agent-a"), ("app-b", "agent-b"))]
    gateway, created, stopped = ChannelManager(SimpleNamespace()), [], []
    stopping, release = asyncio.Event(), asyncio.Event()

    def create(settings):
        channel = DingTalkChannel(DingTalkConfig.model_validate(settings), MagicMock(spec=RobotMessageRouter))
        channel.start = AsyncMock()
        created.append(channel)
        return channel

    async def stop(channel):
        stopped.append(channel)
        if len(stopped) == 1:
            stopping.set()
            await release.wait()

    channels = DingTalkChannelSet(gateway, create_channel=create, stop_channel=stop,
                                  api_base="https://example.invalid", oapi_base="https://example.invalid")
    gateway.set_config_callback(lambda conf: channels.restart())
    await gateway.set_conf("dingtalk", {"apps": apps[:1]})
    # Binding A is still stopping the old channel when binding B saves A and B.
    first = asyncio.create_task(gateway.set_conf("dingtalk", {"apps": apps[:2]}))
    await stopping.wait()
    second = asyncio.create_task(gateway.set_conf("dingtalk", {"apps": apps}))
    await settle(lambda: len(gateway.get_conf("dingtalk")["apps"]) == 3)
    release.set()
    await asyncio.gather(first, second)
    registered = gateway.get_channels_by_id("dingtalk")
    assert sorted(channel.persistent_app_id for channel in registered) == ["app-a", "app-b", "ordinary"]
    # Every started channel is either still registered or was stopped; none keeps running unregistered.
    assert all((channel in registered) != (channel in stopped) for channel in created)


@pytest.mark.parametrize("refuse,code", [("NOT_FOUND", "NOT_FOUND"), (None, "SERVICE_UNAVAILABLE")])
@pytest.mark.asyncio
async def test_nothing_is_saved_when_agent_server_refuses_or_cannot_answer(web, refuse, code):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake"}]})
    web.server.refuse, web.server.running = refuse, refuse is not None
    refused = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-new", "client_secret": "fake", "config_id": "colleague-gone",
    })
    assert refused["ok"] is False and refused["code"] == code
    assert [app["client_id"] for app in web.saved()["apps"]] == ["ordinary"]


@pytest.mark.asyncio
async def test_unbinding_deletes_only_that_jm_app_and_leaves_the_agent_to_agent_server(web):
    web.seed({"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "jm-a", "client_secret": "fake", "persistent_agent_id": "agent-jm-a"},
        {"client_id": "jm-b", "client_secret": "fake", "persistent_agent_id": "agent-jm-b"},
    ]})
    assert (await _call(web, "channel.dingtalk.unbind_agent", {"client_id": "jm-a"}))["ok"]
    # An ordinary app is not a colleague's connection and is left alone.
    assert (await _call(web, "channel.dingtalk.unbind_agent", {"client_id": "ordinary"}))["ok"]
    assert [app["client_id"] for app in web.saved()["apps"]] == ["ordinary", "jm-b"]
    assert web.cm.configs["dingtalk"]["apps"] == web.saved()["apps"]
    assert not web.server.envelopes


@pytest.mark.parametrize("mode", ["unbind", "purge"])
@pytest.mark.asyncio
async def test_deleting_a_colleague_removes_its_apps_before_agent_server_removes_it(web, mode):
    web.seed({"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "jm-a", "client_secret": "fake", "persistent_agent_id": "agent-jm-a"},
        {"client_id": "jm-b", "client_secret": "fake", "persistent_agent_id": "agent-jm-b"},
    ]})
    web.cm.configs["feishu"] = {"apps": [
        {"app_id": "feishu-a", "app_secret": "fake", "persistent_agent_id": "agent-feishu-a"},
        {"app_id": "feishu-other", "app_secret": "fake", "persistent_agent_id": "agent-other"},
    ]}
    web.server.connections = [
        {"channel": "dingtalk", "connection_key": "jm-a", "agent_id": "agent-jm-a"},
        {"channel": "feishu", "connection_key": "feishu-a", "agent_id": "agent-feishu-a"},
    ]
    saved_when_removed = []
    send = web.server.send_request

    async def observe(envelope):
        if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value:
            saved_when_removed.append([app["client_id"] for app in web.saved()["apps"]])
        return await send(envelope)

    web.server.send_request = observe
    deleted = await _call(web, "jiuwenmate.colleague.delete", {"agent_id": "colleague-a", "mode": mode})
    assert deleted["ok"] is True
    steps = [envelope for envelope in web.server.envelopes if envelope.method.startswith("jiuwenmate.colleague.")]
    assert [envelope.method for envelope in steps] == [
        ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value, ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value,
    ]
    assert steps[-1].params == {"config_id": "colleague-a", "mode": mode}
    assert saved_when_removed == [["ordinary", "jm-b"]]
    assert [app["app_id"] for app in web.cm.configs["feishu"]["apps"]] == ["feishu-other"]


@pytest.mark.asyncio
async def test_a_failed_removal_keeps_the_colleague_and_can_be_repeated(web):
    web.seed({"apps": [{"client_id": "jm-a", "client_secret": "fake", "persistent_agent_id": "agent-jm-a"}]})
    web.server.connections = [{"channel": "dingtalk", "connection_key": "jm-a", "agent_id": "agent-jm-a"}]
    send = web.server.send_request

    async def fail_removal(envelope):
        if envelope.method == ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value:
            raise ConnectionError("test AgentServer stopped")
        return await send(envelope)

    web.server.send_request = fail_removal
    failed = await _call(web, "jiuwenmate.colleague.delete", {"agent_id": "colleague-a", "mode": "purge"})
    assert failed["ok"] is False and failed["code"] == "SERVICE_UNAVAILABLE"
    assert web.saved()["apps"] == []
    web.server.send_request = send
    assert (await _call(web, "jiuwenmate.colleague.delete", {"agent_id": "colleague-a", "mode": "purge"}))["ok"]


@pytest.mark.asyncio
async def test_the_default_app_gets_its_waiting_line_and_an_at_handoff_is_not_sent_as_plain_text():
    gateway = ChannelManager(SimpleNamespace())
    default = _dingtalk_channel(client_id="app-default", agent_id="agent-a", is_default=True)
    default.send_persistent_result = AsyncMock(return_value={"outcome": "delivered", "external_message_id": "handle"})
    gateway.register_channel(default)
    notice = await gateway.deliver_thinking_notice({
        "channel": "dingtalk", "app_id": "app-default", "chat_id": "staff-test", "text": "thinking",
        "idempotency_key": "thinking:test",
    })
    assert notice["outcome"] == "delivered"
    default.send_persistent_result.assert_awaited_once()
    handoff = await gateway.deliver_persistent_result({
        "channel": "dingtalk", "app_id": "app-default", "chat_id": "staff-test", "text": "hand over",
        "idempotency_key": "key", "mention_targets": [{"agent_id": "agent-b", "name": "Test colleague"}],
    })
    assert handoff == {"outcome": "failed", "error_code": "DINGTALK_DELIVERY_UNSUPPORTED"}
    assert default.send_persistent_result.await_count == 1


@pytest.mark.asyncio
async def test_gateway_delivery_ack_uses_the_stored_platform():
    handler = object.__new__(MessageHandler)
    handler._result_delivery_handler = AsyncMock(return_value={"outcome": "delivered", "external_message_id": "platform-handle"})
    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True)))
    await handler._handle_result_delivery_push({"delivery_id": "delivery-test", "stage": "send", "channel": "dingtalk"})
    envelope = handler.agent_client.send_request.await_args.args[0]
    assert envelope.channel == "dingtalk"
    assert envelope.params["external_message_id"] == "platform-handle"


def test_restart_only_fences_the_selected_channel_and_never_requeues_unknown(tmp_path):
    from tests.unit_tests.server.jiuwenmate.test_output_files import _accept, _start, _final, NS
    from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot

    repository = ExecutionRepository(tmp_path / "restart.sqlite3")
    state = _accept(repository, "test-source", route=RequestRouteSnapshot(channel="dingtalk", app_id="app-test",
                                                                          target_id="staff-test", source_message_id="source-test"))
    execution = _start(repository, state)
    output = core.AgentEvent.create(execution, event_type=core.EventType.OUTPUT_PROPOSED, local_seq=1, occurred_at=NOW,
        visibility="candidate", payload={"output_id": "out-file", "target_ref": "reply-1", "share_mode": "message", "text": "test output"})
    repository.record_events((output, _final(execution, 2)), now=NOW)
    assert repository.claim_pending_result_deliveries(NS, now=NOW) == []
    jobs = repository.claim_pending_result_deliveries(NS, channels=frozenset({"dingtalk"}), now=NOW)
    assert len(jobs) == 1
    assert repository.recover_sending_result_deliveries(NS, now=NOW) == 0
    assert repository.recover_sending_result_deliveries(NS, channels=frozenset({"dingtalk"}), now=NOW) == 1
    assert repository.result_deliveries(NS, state.record.request_id)[0]["status"] == "unknown"
    assert repository.claim_pending_result_deliveries(NS, channels=frozenset({"dingtalk"}), now=NOW) == []

async def test_welink_membership_validation_ack_preserves_operation_and_evidence():
    handler = object.__new__(MessageHandler)
    handler._result_delivery_handler = AsyncMock(return_value={'outcome': 'unknown', 'handoff_target_valid': True})
    handler.agent_client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True)))
    await handler._handle_result_delivery_push({'delivery_id': 'delivery-test', 'stage': 'send',
        'channel': 'welink', 'operation': 'validate_handoff'})
    envelope = handler.agent_client.send_request.await_args.args[0]
    assert envelope.params['operation'] == 'validate_handoff'
    assert envelope.params['handoff_target_valid'] is True


async def test_multiple_welink_bindings_start_and_recover_shared_runtime(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server.jiuwenmate import feishu_group_fanout as fanout_module, welink_inbound
    from jiuwenswarm.common import config as config_module
    monkeypatch.setattr(config_module, 'get_config', lambda: {})
    monkeypatch.setattr(welink_inbound, 'configured_welink_bindings', lambda config=None: (
        SimpleNamespace(namespace_id='local', agent_id='a'), SimpleNamespace(namespace_id='local', agent_id='b')))
    manager = SimpleNamespace(start=AsyncMock(), recover=AsyncMock(), aclose=AsyncMock())
    fanout = SimpleNamespace()
    monkeypatch.setattr(fanout_module, 'create_default_persistent_group_fanout', lambda **kwargs: (fanout, manager))
    server = object.__new__(server_module.AgentWebSocketServer)
    assert await server._ensure_persistent_group_fanout() is fanout
    manager.recover.assert_awaited_once_with('local', channels=frozenset({'welink'}), include_unrouted=False)
    await server._close_persistent_group_fanout()
