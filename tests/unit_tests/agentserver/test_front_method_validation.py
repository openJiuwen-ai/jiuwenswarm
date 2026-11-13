# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Method validation at the Gateway-facing AgentServer boundary."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import build_fallback_e2a
from jiuwenswarm.common.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.protocol import parse_raw_message


@pytest.mark.parametrize("wire_kind", ["e2a", "legacy", "fallback"])
@pytest.mark.parametrize("method", ["future.extension", "", 0, False])
def test_rejects_unknown_method_at_parse_boundary(wire_kind: str, method: object) -> None:
    if wire_kind == "e2a":
        payload = {"request_id": "unknown-1", "channel": "web", "method": method, "is_stream": True}
    else:
        legacy = {"request_id": "unknown-1", "channel_id": "web", "req_method": method}
        payload = build_fallback_e2a(legacy).to_dict() if wire_kind == "fallback" else legacy

    wire = parse_raw_message(json.dumps(payload))
    assert isinstance(wire, dict)
    response = parse_agent_server_wire_unary(wire)
    assert response.request_id == "unknown-1"
    assert response.ok is False
    assert response.payload["code"] == "METHOD_NOT_FOUND"
    if wire_kind == "e2a":
        chunk = parse_agent_server_wire_chunk(wire)
        assert chunk.is_complete is True
        assert chunk.payload["code"] == "METHOD_NOT_FOUND"


@pytest.mark.parametrize("wire_kind", ["e2a", "legacy", "fallback"])
def test_known_method_stays_enum(wire_kind: str) -> None:
    if wire_kind == "e2a":
        payload = {"request_id": "known-1", "channel": "web", "method": "chat.send"}
    else:
        legacy = {"request_id": "known-1", "channel_id": "web", "req_method": "chat.send"}
        payload = build_fallback_e2a(legacy).to_dict() if wire_kind == "fallback" else legacy

    request = parse_raw_message(json.dumps(payload))
    assert isinstance(request, AgentRequest)
    assert request.req_method is ReqMethod.CHAT_SEND


def test_rejects_invalid_req_method_hidden_by_envelope() -> None:
    wire = parse_raw_message(json.dumps({"request_id": "invalid-1", "req_method": False}))
    assert isinstance(wire, dict)
    assert parse_agent_server_wire_unary(wire).payload["code"] == "METHOD_NOT_FOUND"


def test_absent_method_stays_available_for_internal_requests() -> None:
    request = parse_raw_message(json.dumps({"request_id": "internal-1", "channel": "web"}))
    assert isinstance(request, AgentRequest)
    assert request.req_method is None
