# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import time

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
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


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


def test_envelope_from_dict_preserves_officeclaw_tenant_ids():
    """relay-claw sends top-level agent_id; E2A ingress must not drop it to default/default."""
    env = E2AEnvelope.from_dict(
        {
            "request_id": "relay-req-1",
            "channel_id": "officeclaw",
            "session_id": "officeclaw_sess",
            "agent_id": "office",
            "service_id": "default",
            "params": {"query": "hello"},
            "is_stream": True,
            "req_method": "chat.send",
        }
    )
    assert env.agent_id == "office"
    assert env.service_id == "default"

    req = e2a_to_agent_request(env)
    assert req.agent_id == "office"
    assert req.service_id == "default"


def test_envelope_from_dict_does_not_derive_agent_id_from_agent_ref():
    """agent_ref.id 是路由维，不能提升为租户 agent_id。"""
    env = E2AEnvelope.from_dict(
        {
            "request_id": "r-agent-ref",
            "channel_id": "officeclaw",
            "agent_ref": {"mode": "code", "id": "office"},
            "params": {"query": "hi"},
            "is_stream": True,
            "method": "chat.send",
        }
    )
    assert env.agent_ref == {"mode": "code", "id": "office"}
    assert env.agent_id is None
    req = e2a_to_agent_request(env)
    assert req.agent_id is None
    assert req.agent_ref == {"mode": "code", "id": "office"}


def test_envelope_from_dict_keeps_explicit_top_level_agent_id():
    env = E2AEnvelope.from_dict(
        {
            "request_id": "r-agent-top",
            "channel_id": "officeclaw",
            "agent_id": "office",
            "agent_ref": {"mode": "code", "id": "default"},
            "params": {"query": "hi"},
            "is_stream": True,
            "method": "chat.send",
        }
    )
    assert env.agent_id == "office"
    req = e2a_to_agent_request(env)
    assert req.agent_id == "office"


def test_officeclaw_e2a_tenant_ids_reach_extract_ids():
    env = E2AEnvelope.from_dict(
        {
            "request_id": "relay-req-2",
            "channel_id": "officeclaw",
            "agent_id": "office",
            "params": {"query": "hello"},
            "is_stream": True,
            "method": "chat.send",
        }
    )
    req = e2a_to_agent_request(env)
    agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(req)
    assert agent_id == "office"
    assert service_id == "default"
    assert workspace_key == "default"


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


def test_web_transport_scope_is_bound_to_agent_params():
    """Web：业务 params 不承载 routing；user_id 顶层，group/bot 在 metadata.routing。"""
    env = E2AEnvelope.from_dict(
        {
            "request_id": "web-scope",
            "channel_id": "web",
            "session_id": "s1",
            "user_id": "resolved-user",
            "method": "chat.send",
            "params": {
                "content": "hello",
                "user_id": "payload-user",
                "group_id": "payload-group",
            },
            "metadata": {
                "user_id": "resolved-user",
                "routing": {
                    "group_id": "group-1",
                    "bot_id": "bot-1",
                },
                "query": {
                    "user_id": ["query-user"],
                    "group_id": ["group-1"],
                    "bot_id": ["bot-1"],
                },
            },
        }
    )

    req = e2a_to_agent_request(env)

    # 业务 params 原样保留，不把 handshake routing 写入 params。
    assert req.params["user_id"] == "payload-user"
    assert req.params["group_id"] == "payload-group"
    assert req.params["content"] == "hello"
    assert req.metadata is not None
    assert req.metadata["user_id"] == "resolved-user"
    assert req.metadata["routing"] == {
        "group_id": "group-1",
        "bot_id": "bot-1",
    }


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
