# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import time

from jiuwenclaw.e2a.agent_compat import e2a_to_agent_request
from jiuwenclaw.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
    build_fallback_e2a,
    channel_context_for_channel_reply,
    e2a_from_agent_fields,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
)
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.schema.message import Message, ReqMethod


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
