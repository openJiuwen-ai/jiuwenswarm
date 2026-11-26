# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local method ownership and the real Front / Worker wire boundaries."""

import json
import subprocess
import sys
from dataclasses import fields
from typing import get_args, get_type_hints

import pytest
from e2a.agent_models import AgentRequest as ProtocolAgentRequest
from e2a.message import Message as ProtocolMessage
from e2a.models import E2AEnvelope
from e2a.wire_codec import parse_agent_server_wire_unary

from e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.request_wire import (
    e2a_from_agent_fields,
    message_to_e2a,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.server.edition_gate import ENTERPRISE_ONLY_METHODS
from jiuwenswarm.server.front.protocol import parse_raw_message
from jiuwenswarm.server.ipc.protocol import dump_agent_request, load_agent_request


def test_schema_import_does_not_mutate_protocol_methods():
    result = subprocess.run(
        [sys.executable, "-c", """
from e2a.message import ReqMethod as ProtocolMethod
before = dict(ProtocolMethod.__members__)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.schema.agent import ReqMethod as AgentMethod
assert ReqMethod is AgentMethod
assert ReqMethod is not ProtocolMethod
assert dict(ProtocolMethod.__members__) == before
assert all(ReqMethod[name].value == member.value for name, member in before.items())
assert ReqMethod.SKILLDEV_START.value == 'skilldev.start'
assert ReqMethod.CLIENT_ATTACH.value == 'client.attach'
assert ReqMethod.VIDEO_CONVERSATION_APPEND.value == 'video.conversation.append'
"""],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,wire_value", [
    ("MCP_SOURCES_PROVIDERS", "mcp.sources.providers"),
    ("SESSION_DETACH", "session.detach"),
    ("ASSETS_EXPORT", "assets.export"),
    ("ASSETS_IMPORT", "assets.import"),
])
def test_host_methods_not_in_gateway_catalog_keep_wire_values(name, wire_value):
    assert ReqMethod(wire_value) is ReqMethod[name]


def test_gateway_catalog_methods_have_matching_local_members():
    result = subprocess.run(
        [sys.executable, "-c", """
from jiuwenswarm.common.schema.message import ReqMethod
from agent_gateway.common.protocol_methods import ReqMethod as GatewayMethod
assert ReqMethod is not GatewayMethod
missing = {name: member.value for name, member in GatewayMethod.__members__.items()
           if name not in ReqMethod.__members__}
assert not missing, f'Gateway methods missing locally: {missing}'
mismatched = {name: (member.value, ReqMethod[name].value)
              for name, member in GatewayMethod.__members__.items()
              if member.value != ReqMethod[name].value}
assert not mismatched, f'Gateway/local wire values differ: {mismatched}'
"""],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("local,protocol", [
    (Message, ProtocolMessage), (AgentRequest, ProtocolAgentRequest),
])
def test_request_models_keep_shared_fields_with_local_method_type(local, protocol):
    assert issubclass(local, protocol)
    assert [field.name for field in fields(local)] == [field.name for field in fields(protocol)]
    assert get_args(get_type_hints(local)["req_method"]) == (ReqMethod, str, type(None))


@pytest.mark.parametrize("method", list(ReqMethod), ids=lambda method: method.value)
def test_all_local_methods_roundtrip_through_front_and_worker(method, monkeypatch):
    # Enterprise-only methods pass the front boundary only in an enterprise
    # build; the personal-edition refusal is covered by
    # test_front_method_validation.py.
    if method.value in ENTERPRISE_ONLY_METHODS:
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    envelope = e2a_from_agent_fields(
        request_id="local-method", channel_id="tui", session_id="session",
        req_method=method, params={"content": "hello"}, user_id="owner",
        metadata={"trace": "trace-id"}, is_stream=True,
    )
    assert type(envelope.method) is str
    assert envelope.method == method.value
    request = parse_raw_message(json.dumps(envelope.to_dict()))
    assert isinstance(request, AgentRequest)
    assert request.req_method is method
    payload = dump_agent_request(request)
    assert payload["req_method"] == method.value
    restored = load_agent_request(json.loads(json.dumps(payload)))
    assert restored.req_method is method
    assert restored.params == {"content": "hello"}
    assert restored.user_id == "owner"
    assert restored.metadata["trace"] == "trace-id"
    assert restored.is_stream is True


@pytest.mark.parametrize("method", [ReqMethod.CHAT_SEND, ReqMethod.SKILLDEV_START, None])
@pytest.mark.parametrize("normalize", [message_to_e2a, message_to_e2a_or_fallback])
def test_message_normalization_preserves_caller_and_wire_values(method, normalize):
    message = Message(
        id="message", type="req", channel_id="tui", session_id="session",
        params={"query": "hello"}, timestamp=1.0, ok=True, req_method=method,
    )
    envelope = normalize(message)
    expected = method.value if method else None
    assert envelope.method == expected
    assert message_to_legacy_agent_dict(message)["req_method"] == expected
    assert message.req_method is method
    assert E2A_INTERNAL_CONTEXT_KEY not in (envelope.channel_context or {})


def test_fallback_keeps_local_method_for_legacy_parser():
    message = Message(
        id="", type="req", channel_id="tui", session_id="session",
        params={}, timestamp=0.0, ok=True, req_method=ReqMethod.CLIENT_ATTACH,
    )
    envelope = message_to_e2a_or_fallback(message)
    internal = envelope.channel_context[E2A_INTERNAL_CONTEXT_KEY]
    assert internal[E2A_FALLBACK_FAILED_KEY] is True
    assert internal[E2A_LEGACY_AGENT_REQUEST_KEY]["req_method"] == "client.attach"
    request = parse_raw_message(json.dumps(envelope.to_dict()))
    assert request.req_method is ReqMethod.CLIENT_ATTACH
    assert message.req_method is ReqMethod.CLIENT_ATTACH


def test_unknown_string_reaches_front_method_not_found_response():
    envelope = e2a_from_agent_fields(request_id="unknown", req_method="extension.future")
    assert envelope.method == "extension.future"
    response = parse_agent_server_wire_unary(parse_raw_message(json.dumps(envelope.to_dict())))
    assert response.ok is False
    assert response.payload["code"] == "METHOD_NOT_FOUND"


@pytest.mark.parametrize("method,error", [(123, TypeError), ("", ValueError), ("  ", ValueError)])
def test_invalid_outbound_method_keeps_protocol_validation(method, error):
    with pytest.raises(error):
        e2a_from_agent_fields(request_id="invalid", req_method=method)


def test_gateway_string_request_resolves_to_local_member():
    envelope = E2AEnvelope(request_id="gateway", channel="web", method="jiuwenmate.delivery.ack")
    request = parse_raw_message(json.dumps(envelope.to_dict()))
    assert request.req_method is ReqMethod.JIUWENMATE_DELIVERY_ACK


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "FILE_READ_UPLOAD_CHUNK", "TRAJECTORY_USAGE_GET", "VIDEO_CONVERSATION_APPEND",
    "MODE_SET", "MODE_GET", "SKILLS_UPLOAD_STAGE", "SKILLS_UPLOAD_FINALIZE",
])
async def test_gateway_fetch_restores_host_method_and_routing_identity(name):
    from agent_gateway.common.protocol_methods import ReqMethod as GatewayReqMethod
    from agent_gateway.routing.e2a_proxy import fetch_agent_unary

    from jiuwenswarm.common.schema.agent import AgentResponse

    local_method = ReqMethod[name]
    gateway_method = GatewayReqMethod[name]
    assert gateway_method is not local_method
    requests = []

    class WireClient:
        server_ready = True

        async def send_request(self, envelope):
            assert type(envelope.method) is str
            request = parse_raw_message(json.dumps(envelope.to_dict()))
            requests.append(load_agent_request(dump_agent_request(request)))
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=True, payload={})

    ok, payload = await fetch_agent_unary(
        agent_client=WireClient(), req_method=gateway_method,
        params={"session_id": "session-a"}, session_id="session-a",
        user_id="user-a", channel_id="web",
        metadata={"routing": {"group_id": "group-a", "bot_id": "bot-a"}},
    )
    assert ok and payload == {}
    assert len(requests) == 1
    assert requests[0].req_method is local_method
    assert requests[0].session_id == "session-a"
    assert requests[0].user_id == "user-a"
    assert requests[0].metadata["routing"]["group_id"] == "group-a"
    assert requests[0].metadata["routing"]["bot_id"] == "bot-a"
