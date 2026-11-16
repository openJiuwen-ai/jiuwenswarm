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

from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
    e2a_from_agent_fields,
    message_to_e2a,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import Message, ReqMethod
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
def test_all_local_methods_roundtrip_through_front_and_worker(method):
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
