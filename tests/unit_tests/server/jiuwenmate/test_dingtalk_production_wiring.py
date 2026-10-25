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
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition, configured_feishu_bindings
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
    inbound = DingTalkInboundHandler(service=service, sessions=repository, agent_manager=manager, config_loader=lambda: config)
    await manager.start()
    yield SimpleNamespace(config=config, repository=repository, service=service, manager=manager, inbound=inbound,
        contexts=contexts, sent=sent, gateway=gateway, deliveries=deliveries, workspaces=workspaces, channel=channel)
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
async def test_manual_feishu_reuse_is_rejected_by_dingtalk_without_changing_feishu(host):
    host.config["channels"]["feishu"]["apps"][0]["persistent_agent_id"] = "agent-dingtalk"
    assert await host.inbound.handle(_event()) is InboundDisposition.REJECTED
    assert [item.agent_id for item in configured_feishu_bindings(host.config)] == ["agent-dingtalk"]
    assert not host.contexts


@pytest.mark.parametrize("conversation", [
    ConversationRef.im_direct("feishu", "old-chat"),
    ConversationRef.group("group-test"),
    ConversationRef.im_direct("dingtalk", json.dumps(["other-dingtalk-app", "old-conversation"])),
])
@pytest.mark.asyncio
async def test_an_agent_that_served_another_connection_is_not_rebound(host, conversation):
    host.repository.resolve_session("local", "agent-dingtalk", conversation, now=NOW)
    assert await host.inbound.handle(_event()) is InboundDisposition.REJECTED
    assert not host.contexts


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
    """Answers Gateway's binding check; ``allowed=None`` behaves like an unreachable server."""

    def __init__(self, allowed=True):
        self.allowed = allowed
        self.envelopes = []
        self.gate = None

    async def send_request(self, envelope):
        self.envelopes.append(envelope)
        gate, self.gate = self.gate, None
        if gate is not None:
            await gate.wait()
        if self.allowed is None:
            raise ConnectionError("test AgentServer is not running")
        return SimpleNamespace(ok=True, payload={"allowed": self.allowed})


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
        "client_id": "app-dingtalk", "client_secret": "fake-secret", "persistent_agent_id": "agent-dingtalk",
    })
    assert bound["ok"] is True
    saved = web.saved()
    assert "client_id" not in saved and saved["last_sender_id"] == "staff-old"
    assert [app["client_id"] for app in saved["apps"]] == ["ordinary", "app-dingtalk"]
    assert saved["apps"][1]["persistent_agent_id"] == "agent-dingtalk"
    edited = await _call(web, "channel.dingtalk.set_conf", {"apps": [
        {"client_id": "ordinary", "allow_from": ["staff-test"], "enabled": True},
        {"client_id": "app-dingtalk", "enabled": False},
    ]})
    assert edited["ok"] is True
    ordinary, jm = web.saved()["apps"]
    assert ordinary["allow_from"] == ["staff-test"] and ordinary["client_secret"] == "fake-old"
    assert jm["persistent_agent_id"] == "agent-dingtalk" and jm["client_secret"] == "fake-secret" and jm["enabled"] is False
    assert web.cm.configs["dingtalk"] == dict(web.saved())
    listing = await _call(web, "channel.dingtalk.get_conf", {})
    assert [app["client_id"] for app in listing["payload"]["config"]["apps"]] == ["ordinary", "app-dingtalk"]


@pytest.mark.asyncio
async def test_deleting_one_app_keeps_the_others_and_rebinding_replaces_only_that_colleague(web):
    web.seed({"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "jm-a", "client_secret": "fake", "persistent_agent_id": "agent-a"},
        {"client_id": "jm-b", "client_secret": "fake", "persistent_agent_id": "agent-b"},
    ], "last_sender_id": "staff-old"})
    assert (await _call(web, "channel.dingtalk.set_conf", {"apps": [{"client_id": "jm-a"}, {"client_id": "jm-b"}]}))["ok"]
    saved = web.saved()
    assert [app["client_id"] for app in saved["apps"]] == ["jm-a", "jm-b"] and "last_sender_id" not in saved
    rebound = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "jm-c", "client_secret": "fake", "persistent_agent_id": "agent-a",
    })
    assert rebound["ok"] is True
    assert [(app["client_id"], app["persistent_agent_id"]) for app in web.saved()["apps"]] == [
        ("jm-b", "agent-b"), ("jm-c", "agent-a"),
    ]


@pytest.mark.asyncio
async def test_dingtalk_binding_refuses_an_agent_bound_to_feishu_and_a_second_ordinary_app(web):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake"}]})
    web.cm.configs["feishu"] = {"apps": [{"app_id": "feishu-app", "persistent_agent_id": "same-agent"}]}
    refused = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app", "client_secret": "fake", "persistent_agent_id": "same-agent",
    })
    assert refused["ok"] is False and refused["code"] == "BAD_REQUEST"
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
        "client_id": "app-a", "client_secret": "fake", "persistent_agent_id": "agent-a",
    }, "test-session"))
    # Binding A waits in the AgentServer check while binding B completes.
    await settle(lambda: len(web.server.envelopes) == 1)
    second = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-b", "client_secret": "fake", "persistent_agent_id": "agent-b",
    })
    assert second["ok"] is True and not first.done()
    gate.set()
    await first
    assert [response["ok"] for response in web.channel.responses] == [True, True]
    assert [(app["client_id"], app["persistent_agent_id"]) for app in web.saved()["apps"]] == [
        ("ordinary", ""), ("app-b", "agent-b"), ("app-a", "agent-a"),
    ]
    assert web.cm.configs["dingtalk"]["apps"] == web.saved()["apps"]


@pytest.mark.asyncio
async def test_a_malformed_allowlist_survives_binding_and_blocks_a_client_id_change(web):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake", "allow_from": "staff-only"}]})
    bound = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-a", "client_secret": "fake", "persistent_agent_id": "agent-a",
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


@pytest.mark.parametrize("allowed,code", [(False, "BAD_REQUEST"), (None, "SERVICE_UNAVAILABLE")])
@pytest.mark.asyncio
async def test_binding_is_refused_when_the_colleague_served_another_connection_or_cannot_be_checked(web, allowed, code):
    web.seed({"apps": [{"client_id": "ordinary", "client_secret": "fake"}]})
    web.server.allowed = allowed
    refused = await _call(web, "channel.dingtalk.bind_agent", {
        "client_id": "app-new", "client_secret": "fake", "persistent_agent_id": "agent-used",
    })
    assert refused["ok"] is False and refused["code"] == code
    envelope = web.server.envelopes[-1]
    assert envelope.method == ReqMethod.JIUWENMATE_DINGTALK_BINDING_CHECK.value
    assert envelope.params == {"agent_id": "agent-used", "app_id": "app-new"}
    assert [app["client_id"] for app in web.saved()["apps"]] == ["ordinary"]


@pytest.mark.asyncio
async def test_agent_server_answers_the_binding_check_from_existing_sessions(tmp_path, monkeypatch):
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.server.jiuwenmate import service as service_module

    monkeypatch.setattr(service_module, "default_data_dir", lambda: tmp_path)
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    repository.resolve_session("local", "agent-used", ConversationRef.im_direct("feishu", "old-chat"), now=NOW)
    repository.resolve_session("local", "agent-same", ConversationRef.im_direct(
        "dingtalk", json.dumps(["app-dingtalk", "conversation-test"], separators=(",", ":")),
    ), now=NOW)
    responses = []
    monkeypatch.setattr(module, "encode_agent_response_for_wire", lambda response, response_id: response)
    monkeypatch.setattr(module, "send_wire_payload", AsyncMock(side_effect=lambda ws, response: responses.append(response)))
    server = object.__new__(module.AgentWebSocketServer)
    for agent_id in ("agent-used", "agent-same", "agent-new"):
        request = e2a_to_agent_request(E2AEnvelope(request_id=f"check-{agent_id}", channel="web",
            method=ReqMethod.JIUWENMATE_DINGTALK_BINDING_CHECK.value,
            params={"agent_id": agent_id, "app_id": "app-dingtalk"}))
        await server.dispatch_parsed_request(object(), request, asyncio.Lock())
    assert [response.payload for response in responses] == [{"allowed": False}, {"allowed": True}, {"allowed": True}]


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
