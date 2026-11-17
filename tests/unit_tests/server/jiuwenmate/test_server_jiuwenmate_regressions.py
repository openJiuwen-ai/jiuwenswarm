# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 4 small test modules)."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
import pytest
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from jiuwenswarm.server.jiuwenmate import web_adapter
from tests.unit_tests.server.jiuwenmate.test_permission_approvals import NOW, paused_request
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import Host
from pathlib import Path
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope
import json
from agent_gateway.app_gateway import RouteConfig
from agent_gateway.channel_manager.web.app_web_handlers import (
    _FORWARD_NO_LOCAL_HANDLER_METHODS, _FORWARD_REQ_METHODS,
)
from tests.unit_tests.gateway.test_app_gateway_acp import build_server, FakeWebSocket
from datetime import datetime, timezone
from e2a.message import ReqMethod as WireMethod
from e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkInboundEvent as HostDingTalkEvent
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import FeishuInboundEvent as HostFeishuEvent


# --- from test_approval_runtime_ownership.py ---

@pytest.mark.parametrize("channels,channel,expected", [
    (frozenset({"web"}), "feishu", False),
    (frozenset({"web"}), "dingtalk", False),
    (frozenset({"web"}), "web", True),
    (frozenset({"feishu", "dingtalk", "welink"}), "web", False),
    (frozenset({"feishu", "dingtalk", "welink"}), "feishu", True),
    (frozenset({"feishu", "dingtalk", "welink"}), "dingtalk", True),
])
async def test_resume_is_owned_by_the_original_channel_runtime(tmp_path, channels, channel, expected):
    manager = Host(tmp_path).manager
    manager._approval_channels = channels
    manager._resume_permission_approval = AsyncMock()
    state, event = paused_request(channel="web" if channel == "web" else "feishu",
                                 app_id=None if channel == "web" else "app-1")
    state = replace(state, record=replace(state.record, route_snapshot=replace(state.record.route_snapshot, channel=channel)))
    manager._sessions.get_request = Mock(return_value=state)
    approval = PermissionApprovalRepository(tmp_path / "approval.sqlite3").create(state, event, now=NOW)
    manager._schedule_permission_resume(approval)
    await asyncio.gather(*manager._permission_resume_tasks.values())
    assert manager._resume_permission_approval.await_count == int(expected)

def test_production_web_factory_claims_only_web_approvals(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr(web_adapter.AgentManager, "create_default", factory)
    observer = Mock()
    web_adapter._default_manager(object(), on_event=observer)
    assert factory.call_args.kwargs["approval_channels"] == frozenset({"web"})

# --- from test_dingtalk_cli_app_scope.py ---

def runtime(tmp_path):
    apps = [
        {"client_id": "default-app", "client_secret": "default-secret", "is_default": True,
         "enabled": True, "allow_from": []},
        {"client_id": "agent-app", "client_secret": "agent-secret", "persistent_agent_id": "agent",
         "enabled": True, "allow_from": []},
    ]
    return LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: {"channels": {"dingtalk": {"apps": apps}}},
        dws_path=Path("/tools/dws"),
    ), apps

def test_trusted_route_app_overrides_a_different_default(tmp_path):
    provider, _ = runtime(tmp_path)
    assert provider._binding(DingTalkCliScope("local", "agent", app_id="agent-app")) == (
        "agent-app", "agent-secret",
    )
    assert provider._binding(DingTalkCliScope("local", "another-agent")) == ("default-app", "default-secret")

@pytest.mark.parametrize("change", ["missing", "disabled", "duplicate", "wrong-agent"])
def test_explicit_app_failure_never_falls_back_to_default(tmp_path, change):
    provider, apps = runtime(tmp_path)
    if change == "missing":
        apps.pop()
    elif change == "disabled":
        apps[1]["enabled"] = False
    elif change == "duplicate":
        apps.append(dict(apps[1]))
    else:
        apps[1]["persistent_agent_id"] = "another-agent"
    assert provider._binding(DingTalkCliScope("local", "agent", app_id="agent-app")) is None

# --- from test_group_sharing_gateway.py ---

@pytest.mark.parametrize("method", ["jiuwenmate.sharing.list", "jiuwenmate.sharing.decide", "jiuwenmate.sharing.revoke"])
async def test_sharing_methods_are_registered_and_forwarded_to_agent_server(method):
    server = build_server()
    server.config.routes["/web"] = RouteConfig(
        path="/web", channel_id="web", forward_methods=_FORWARD_REQ_METHODS,
        forward_no_local_handler_methods=_FORWARD_NO_LOCAL_HANDLER_METHODS,
    )
    seen = []

    async def receive(message):
        seen.append(message)

    server.on_message(receive)
    ws = FakeWebSocket()
    await server.handle_raw_message_public(ws, json.dumps({
        "type": "req", "id": "sharing-rpc", "method": method,
        "params": {"agent_id": "config-test", "session_id": "session-test"},
    }), path="/web")
    assert len(seen) == 1
    assert seen[0].req_method.value == method
    assert seen[0].params["agent_id"] == "config-test"
    assert not ws.sent_frames

# --- from test_split_gateway_contract.py ---

@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
def test_gateway_group_event_and_receipt_decode_on_host(channel):
    from agent_gateway.channel_manager.persistent_events import DingTalkInboundEvent, FeishuInboundEvent

    fields = dict(receiving_app_id="app", expected_agent_id="mate", message_id="source-msg",
                  sender_name="Alice", text="agenda", received_at=datetime.now(timezone.utc))
    if channel == "feishu":
        source = FeishuInboundEvent(**fields, chat_id="group", chat_type="group", sender_type="user",
                                    sender_id="alice", sender_app_id=None)
        parse = HostFeishuEvent.from_payload
    else:
        source = DingTalkInboundEvent(**fields, conversation_id="group", conversation_type="2",
                                      sender_staff_id="alice")
        parse = HostDingTalkEvent.from_payload

    def request(method, params):
        wire = E2AEnvelope(channel=channel, chat_id="group", request_id="request", method=method, params=params)
        return e2a_to_agent_request(E2AEnvelope.from_dict(wire.to_dict()))

    inbound = request(f"jiuwenmate.{channel}.inbound", {"event": source.to_payload()})
    assert inbound.req_method is WireMethod(f"jiuwenmate.{channel}.inbound")
    assert parse(inbound.params["event"]).to_payload() == source.to_payload()
    receipt = request("jiuwenmate.delivery.ack", {"delivery_id": "delivery", "stage": "send",
                                                 "outcome": "delivered", "external_message_id": "sent"})
    assert receipt.req_method is ReqMethod.JIUWENMATE_DELIVERY_ACK
    assert receipt.params["delivery_id"] == "delivery"

