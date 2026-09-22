# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import time
import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
    build_fallback_e2a,
    channel_context_for_channel_reply,
    e2a_from_agent_fields,
    e2a_response_to_agent_chunk,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope, E2AResponse
from jiuwenswarm.common.schema.message import Message, ReqMethod


@pytest.mark.parametrize("event_type", ["chat.delta", "chat.reasoning", "chat.final", "runtime.accepted"])
def test_execution_binding_survives_agent_wire_and_web_payload(event_type):
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_from_agent_chunk
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    chunk = AgentResponseChunk(
        request_id="request-A", channel_id="web", is_complete=event_type == "chat.final",
        payload={"event_type": event_type, "execution_id": "execution-A", "content": "text"},
    )
    wire = e2a_response_from_agent_chunk(chunk, response_id="request-A", sequence=1)
    restored = e2a_response_to_agent_chunk(wire)
    message = Message(
        id=restored.request_id, type="event", channel_id="web", session_id="session-A",
        params={}, timestamp=1, ok=True, payload=restored.payload,
    )
    payload = WebChannel._build_event_payload(message, event_type)
    assert payload["execution_id"] == "execution-A"
    assert payload["session_id"] == "session-A"


@pytest.mark.parametrize(
    ("event_type", "is_complete"),
    [("chat.delta", False), ("chat.final", False), ("chat.final", True)],
)
def test_cross_session_identity_survives_agent_wire_and_web_payload(event_type, is_complete):
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_from_agent_chunk
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    fields = {
        "turn_request_id": "turn-A",
        "message_origin": "cross_session_agent",
        "session_message_id": "sm-A",
        "cross_session": {"message_id": "sm-A", "source_session_id": "source-A"},
    }
    chunk = AgentResponseChunk(
        request_id="wire-A", channel_id="web", is_complete=is_complete,
        payload={"event_type": event_type, "content": "text", **fields},
    )
    wire = e2a_response_from_agent_chunk(chunk, response_id="wire-A", sequence=1)
    restored = e2a_response_to_agent_chunk(wire)
    message = Message(
        id=restored.request_id, type="event", channel_id="web", session_id="target-A",
        params={}, timestamp=1, ok=True, payload=restored.payload,
    )
    payload = WebChannel._build_event_payload(message, event_type)
    assert {key: payload.get(key) for key in fields} == fields


@pytest.mark.parametrize("event_type", ["chat.delta", "chat.reasoning", "chat.final", "chat.tool_call", "chat.input_received", "chat.output_phase"])
def test_phase_boundary_and_order_survive_both_wire_conversions(event_type):
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_from_agent_chunk
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    fields = {"output_phase_id": "phase-1", "output_suppressed": True,
              "output_order": {"request_id": "original", "sequence": 42}, "timestamp": 1800000000042}
    if event_type == "chat.input_received":
        fields["input_request_id"] = "input-1"
    if event_type == "chat.output_phase":
        fields["applied_input_ids"] = ["input-1", "input-2"]
    chunk = AgentResponseChunk(request_id="original", channel_id="web", is_complete=False,
                               payload={"event_type": event_type, "content": "text", **fields})
    restored = e2a_response_to_agent_chunk(e2a_response_from_agent_chunk(chunk, response_id="original", sequence=42))
    message = Message(id="original", type="event", channel_id="web", session_id="session",
                      params={}, timestamp=1, ok=True, payload=restored.payload)
    payload = WebChannel._build_event_payload(message, event_type)
    assert {key: payload.get(key) for key in fields} == fields


def test_message_to_e2a_or_fallback_basic():
    msg = Message(
        id="r1",
        type="req",
        channel_id="web",
        session_id="s1",
        params={"query": "hi"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=False,
        metadata={"method": "chat.send", "query": {}},
    )
    env = message_to_e2a_or_fallback(msg)
    assert env.request_id == "r1"
    assert env.channel == "web"
    assert env.method == "chat.send"
    assert env.params == {"query": "hi"}
    assert env.channel_context.get("method") == "chat.send"


def test_envelope_from_dict_merges_metadata_when_channel_context_nonempty():
    """telemetry 等先写入 channel_context 时，顶层 metadata 仍须并入，以便 AgentRequest.metadata 含 wecom_chat_id。"""
    env = E2AEnvelope.from_dict(
        {
            "request_id": "r3",
            "channel_id": "wecom",
            "session_id": "s3",
            "params": {"query": "q"},
            "is_stream": True,
            "method": "chat.send",
            "channel_context": {"traceparent": "00-abc-def-01"},
            "metadata": {"wecom_chat_id": "user1"},
        }
    )
    req = e2a_to_agent_request(env)
    assert req.metadata["traceparent"] == "00-abc-def-01"
    assert req.metadata["wecom_chat_id"] == "user1"


def test_e2a_to_agent_request_roundtrip():
    msg = Message(
        id="r2",
        type="req",
        channel_id="wecom",
        session_id="s2",
        params={"content": "x"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        metadata={"wecom_req_id": "abc"},
    )
    env = message_to_e2a_or_fallback(msg)
    req = e2a_to_agent_request(env)
    assert req.request_id == "r2"
    assert req.channel_id == "wecom"
    assert req.req_method == ReqMethod.CHAT_SEND
    assert req.metadata == {"wecom_req_id": "abc"}


def test_e2a_to_agent_request_preserves_user_id_for_agentos_routing():
    env = e2a_from_agent_fields(
        request_id="cron-snapshot",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CRON_JOBS_SYNC,
        params={"jobs": []},
        user_id="alice",
    )

    req = e2a_to_agent_request(env)

    assert req.user_id == "alice"


def test_message_to_e2a_or_fallback_preserves_user_id():
    msg = Message(
        id="r-user",
        type="req",
        channel_id="tui",
        session_id="s1",
        params={"content": "hi"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        user_id="alice",
    )
    env = message_to_e2a_or_fallback(msg)
    assert env.user_id == "alice"
    assert e2a_to_agent_request(env).user_id == "alice"


def test_e2a_from_agent_fields_user_id():
    env = e2a_from_agent_fields(
        request_id="r1",
        channel_id="tui",
        session_id="s1",
        req_method=ReqMethod.CHAT_SEND,
        user_id="bob",
    )
    assert env.user_id == "bob"


def test_channel_context_for_channel_reply_strips_internal():
    env = e2a_from_agent_fields(
        request_id="x",
        channel_id="web",
        metadata={"a": 1},
    )
    env.channel_context[E2A_INTERNAL_CONTEXT_KEY] = {E2A_FALLBACK_FAILED_KEY: True}
    out = channel_context_for_channel_reply(env)
    assert out == {"a": 1}
    assert E2A_INTERNAL_CONTEXT_KEY not in out


def test_build_fallback_and_legacy_keys():
    legacy = message_to_legacy_agent_dict(
        Message(
            id="fb",
            type="req",
            channel_id="web",
            session_id="s",
            params={"k": 1},
            timestamp=1.0,
            ok=True,
            req_method=ReqMethod.HISTORY_GET,
        )
    )
    env = build_fallback_e2a(legacy)
    inner = env.channel_context[E2A_INTERNAL_CONTEXT_KEY]
    assert inner[E2A_FALLBACK_FAILED_KEY] is True
    assert inner[E2A_LEGACY_AGENT_REQUEST_KEY]["req_method"] == "history.get"


def test_e2a_response_to_agent_chunk_plan_approval_required():
    e2a = E2AResponse(
        response_id="req-plan-1",
        request_id="req-plan-1",
        sequence=0,
        is_final=True,
        status="succeeded",
        response_kind=E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED,
        body={
            "plan_content": "## Plan\nDo the thing",
            "plan_slug": "bright-otter",
            "plan_path": "/tmp/.plans/bright-otter.md",
        },
        channel="tui",
        session_id="session-1",
    )
    chunk = e2a_response_to_agent_chunk(e2a)
    assert chunk.payload["event_type"] == "plan.approval_required"
    assert chunk.payload["plan_content"] == "## Plan\nDo the thing"
    assert chunk.payload["plan_slug"] == "bright-otter"
    assert chunk.is_complete is True
