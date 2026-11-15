"""Implementation-owned methods resolve only at the AgentServer boundary."""

import json
import asyncio
from enum import StrEnum
from typing import get_type_hints
from unittest.mock import AsyncMock

import pytest

import e2a
from e2a.gateway_normalize import e2a_from_agent_fields
from e2a.models import E2AEnvelope
from jiuwenswarm.common.schema import agent, message
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.protocol import parse_raw_message


def test_method_vocabulary_is_owned_by_swarm():
    assert not hasattr(e2a, "ReqMethod")
    assert not hasattr(__import__("e2a.message", fromlist=["Message"]), "ReqMethod")
    assert agent.ReqMethod is message.ReqMethod is ReqMethod
    assert issubclass(ReqMethod, StrEnum)
    assert get_type_hints(agent.AgentRequest)["req_method"] == str | None


def test_every_swarm_method_survives_wire_and_resolves_at_front():
    for method in ReqMethod:
        envelope = e2a_from_agent_fields(
            request_id="method-contract", channel_id="web", req_method=method,
            params={"nested": [1, False, None]}, is_stream=False,
        )
        wire = json.loads(json.dumps(envelope.to_dict()))
        assert type(wire["method"]) is str
        assert wire["method"] == method.value
        request = parse_raw_message(json.dumps(wire))
        assert request.req_method is method
        assert request.params == {"nested": [1, False, None]}


def test_unimplemented_method_reaches_front_and_returns_method_not_found():
    envelope = E2AEnvelope(
        request_id="future-request", channel="web", method="future.not_implemented",
        params={"opaque": {"value": 42}},
    )
    wire = json.dumps(envelope.to_dict())
    assert E2AEnvelope.from_dict(json.loads(wire)).method == "future.not_implemented"
    response = parse_raw_message(wire)
    assert response["request_id"] == "future-request"
    assert "METHOD_NOT_FOUND" in json.dumps(response)


@pytest.mark.parametrize("method", ["client.attach", "future.not_implemented"])
async def test_gateway_bus_reaches_swarm_front_without_method_registration(method):
    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from e2a.gateway_normalize import message_to_e2a

    bus = RobotMessageRouter()
    channel = WebChannel(WebChannelConfig(enabled=True), bus)
    channel.send_response = AsyncMock()
    peer = type("Peer", (), {"remote_address": ("127.0.0.1", 1), "closed": False})()
    params = {"session_id": "contract-session", "opaque": {"values": [1, False, None]}}
    try:
        await channel._handle_raw_message(peer, json.dumps({
            "type": "req", "id": "contract", "method": method, "params": params,
        }), {})
        forwarded = await asyncio.wait_for(bus.wait_for_user_message(), timeout=1)
        assert type(forwarded.req_method) is str
        assert forwarded.params == params
        channel.send_response.assert_not_awaited()
        response = parse_raw_message(json.dumps(message_to_e2a(forwarded).to_dict()))
        if method == "client.attach":
            assert response.req_method is ReqMethod.CLIENT_ATTACH
            assert response.params == params
        else:
            assert response["request_id"] == "contract"
            assert "METHOD_NOT_FOUND" in json.dumps(response)
    finally:
        await channel.unregister_ws(peer)
