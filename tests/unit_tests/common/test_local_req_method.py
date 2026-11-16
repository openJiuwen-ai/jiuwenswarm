# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request schemas and conversions must not consult the protocol method enum."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
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


def _message(method: ReqMethod | str | None) -> Message:
    return Message(
        id="local-method", type="req", channel_id="web", session_id="session",
        params={"content": "hello"}, timestamp=1.0, ok=True, req_method=method,
        user_id="owner", is_stream=True, metadata={"trace": "trace-1"},
    )


@pytest.mark.parametrize("model", [Message, AgentRequest])
def test_request_models_declare_the_local_method_type(model: type) -> None:
    assert model.__module__.startswith("jiuwenswarm.")
    assert set(get_args(get_type_hints(model)["req_method"])) == {ReqMethod, str, type(None)}


@pytest.mark.parametrize("method", list(ReqMethod), ids=lambda method: method.name)
def test_every_local_method_round_trips_through_e2a_and_ipc(method: ReqMethod) -> None:
    message = _message(method)
    envelope = message_to_e2a(message)
    assert type(envelope.method) is str
    assert envelope.method == method.value
    assert json.loads(json.dumps(envelope.to_dict()))["method"] == method.value
    request = e2a_to_agent_request(envelope)
    assert type(request) is AgentRequest
    assert request.req_method is method
    assert request.params == message.params
    assert request.metadata["trace"] == "trace-1"
    assert request.user_id == "owner"
    restored = load_agent_request(json.loads(json.dumps(dump_agent_request(request))))
    assert restored.req_method is method
    assert restored.request_id == message.id
    assert message.req_method is method


@pytest.mark.parametrize("wire_kind", ["e2a", "legacy", "fallback"])
def test_swarm_only_method_reaches_front_as_local_enum(wire_kind: str) -> None:
    message = _message(ReqMethod.CLIENT_ATTACH)
    if wire_kind == "legacy":
        payload = message_to_legacy_agent_dict(message)
    else:
        if wire_kind == "fallback":
            message.id = ""
        payload = message_to_e2a_or_fallback(message).to_dict()
    request = parse_raw_message(json.dumps(payload))
    assert isinstance(request, AgentRequest)
    assert request.req_method is ReqMethod.CLIENT_ATTACH


@pytest.mark.parametrize("method", [None, "future.extension", ReqMethod.CLIENT_ATTACH])
def test_field_and_legacy_conversion_preserve_method_values(method: ReqMethod | str | None) -> None:
    expected = method.value if isinstance(method, ReqMethod) else method
    assert e2a_from_agent_fields(request_id="fields", req_method=method).method == expected
    assert message_to_legacy_agent_dict(_message(method))["req_method"] == expected
    assert message_to_e2a_or_fallback(_message(method)).method == expected


@pytest.mark.parametrize("method, error", [("", ValueError), (" ", ValueError), (False, TypeError), (3, TypeError)])
def test_invalid_methods_are_not_hidden_by_fallback(method: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        message_to_e2a_or_fallback(_message(method))
    with pytest.raises(error):
        e2a_from_agent_fields(request_id="invalid", req_method=method)


def test_fallback_snapshot_contains_wire_string() -> None:
    message = replace(_message(ReqMethod.CLIENT_ATTACH), id="")
    fallback = message_to_e2a_or_fallback(message)
    internal = fallback.channel_context[E2A_INTERNAL_CONTEXT_KEY]
    assert internal[E2A_FALLBACK_FAILED_KEY] is True
    assert type(internal[E2A_LEGACY_AGENT_REQUEST_KEY]["req_method"]) is str
    assert internal[E2A_LEGACY_AGENT_REQUEST_KEY]["req_method"] == "client.attach"


def test_request_path_works_without_protocol_method_catalog() -> None:
    script = textwrap.dedent("""
        from enum import Enum
        import e2a.message as protocol_message
        import e2a.agent_models as protocol_agent
        import e2a.gateway_normalize as protocol_normalize
        import agent_gateway

        class EmptyMethod(Enum):
            pass

        def unavailable(*args, **kwargs):
            raise AssertionError("protocol request conversion must not run")

        for module in (protocol_message, protocol_agent, protocol_normalize):
            module.ReqMethod = EmptyMethod
        for name in ("method_value", "message_to_e2a", "message_to_e2a_or_fallback",
                     "message_to_legacy_agent_dict", "e2a_from_agent_fields"):
            setattr(protocol_normalize, name, unavailable)

        from jiuwenswarm.common.schema.message import Message, ReqMethod
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a_or_fallback
        from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
        message = Message(id="isolated", type="req", channel_id="web", session_id=None,
                          params={}, timestamp=0, ok=True, req_method=ReqMethod.CLIENT_ATTACH)
        request = e2a_to_agent_request(message_to_e2a_or_fallback(message))
        assert type(request) is AgentRequest
        assert request.req_method is ReqMethod.CLIENT_ATTACH
        assert not EmptyMethod.__members__
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
