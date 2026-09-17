# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""E2A WebSocket 线编码 / 解码与 round-trip。"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from openjiuwen.core.session.stream import OutputSchema

from jiuwenswarm.common.e2a.constants import E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY
from jiuwenswarm.common.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_from_agent_response,
    e2a_response_to_agent_chunk,
    e2a_response_to_agent_response,
)
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk


def test_roundtrip_unary_ok() -> None:
    orig = AgentResponse(
        request_id="r1",
        channel_id="c1",
        ok=True,
        payload={"a": 1},
        metadata={"m": 2},
    )
    wire = encode_agent_response_for_wire(orig, response_id="r1")
    back = parse_agent_server_wire_unary(wire)
    assert back.request_id == orig.request_id
    assert back.channel_id == orig.channel_id
    assert back.ok is True
    assert back.payload == orig.payload
    assert back.metadata == orig.metadata


def test_roundtrip_unary_error() -> None:
    orig = AgentResponse(
        request_id="r2",
        channel_id="c2",
        ok=False,
        payload={"error": "x", "code": 9},
    )
    wire = encode_agent_response_for_wire(orig, response_id="r2")
    back = parse_agent_server_wire_unary(wire)
    assert back.ok is False
    assert back.payload == orig.payload


def test_encode_unary_with_nested_output_schema_is_json_serializable() -> None:
    orig = AgentResponse(
        request_id="approval-answer",
        channel_id="web",
        ok=True,
        payload={
            "result": OutputSchema(
                type="answer",
                index=0,
                payload={"output": "approval accepted", "result_type": "answer"},
            )
        },
    )

    wire = encode_agent_response_for_wire(orig, response_id="approval-answer")

    json.dumps(wire, ensure_ascii=False)
    back = parse_agent_server_wire_unary(wire)
    assert back.payload == {
        "result": {
            "type": "answer",
            "index": 0,
            "payload": {"output": "approval accepted", "result_type": "answer"},
        }
    }


def test_roundtrip_chunk_sentinel_complete() -> None:
    orig = AgentResponseChunk(
        request_id="s1",
        channel_id="c",
        payload={"is_complete": True},
        is_complete=True,
    )
    wire = encode_agent_chunk_for_wire(orig, response_id="s1", sequence=0)
    back = parse_agent_server_wire_chunk(wire)
    assert back.is_complete is True
    assert back.payload == {"is_complete": True}


def test_roundtrip_chunk_chat_delta() -> None:
    orig = AgentResponseChunk(
        request_id="s2",
        channel_id="c",
        payload={
            "event_type": "chat.delta",
            "content": "hi",
            "source_chunk_type": "llm_reasoning",
        },
        is_complete=False,
    )
    wire = encode_agent_chunk_for_wire(orig, response_id="s2", sequence=1)
    back = parse_agent_server_wire_chunk(wire)
    assert back.is_complete is False
    assert back.payload.get("event_type") == "chat.delta"
    assert back.payload.get("content") == "hi"
    assert back.payload.get("source_chunk_type") == "llm_reasoning"


def test_roundtrip_chunk_custom_event() -> None:
    orig = AgentResponseChunk(
        request_id="s3",
        channel_id="c",
        payload={"event_type": "history.message", "message": {"id": 1}},
        is_complete=False,
    )
    wire = encode_agent_chunk_for_wire(orig, response_id="s3", sequence=0)
    back = parse_agent_server_wire_chunk(wire)
    assert back.payload.get("event_type") == "history.message"
    assert back.payload.get("message") == {"id": 1}


def test_roundtrip_chunk_chat_error() -> None:
    orig = AgentResponseChunk(
        request_id="s4",
        channel_id="c",
        payload={"event_type": "chat.error", "error": "boom"},
        is_complete=True,
    )
    wire = encode_agent_chunk_for_wire(orig, response_id="s4", sequence=2)
    back = parse_agent_server_wire_chunk(wire)
    assert back.is_complete is True
    assert back.payload.get("event_type") == "chat.error"
    assert back.payload.get("error") == "boom"


def test_deprecated_legacy_unary_dict() -> None:
    d = {
        "request_id": "old",
        "channel_id": "ch",
        "ok": True,
        "payload": {"x": 1},
    }
    back = parse_agent_server_wire_unary(d)
    assert back.request_id == "old"
    assert back.payload == {"x": 1}


def test_deprecated_legacy_chunk_dict() -> None:
    d = {
        "request_id": "oldc",
        "channel_id": "ch",
        "payload": {"content": "z"},
        "is_complete": False,
    }
    back = parse_agent_server_wire_chunk(d)
    assert back.request_id == "oldc"
    assert back.payload == {"content": "z"}


def test_parse_unary_prefers_metadata_legacy_blob() -> None:
    legacy = asdict(
        AgentResponse(
            request_id="blob",
            channel_id="c",
            ok=True,
            payload={"recovered": True},
        )
    )
    e2a = e2a_response_from_agent_response(
        AgentResponse(
            request_id="blob",
            channel_id="c",
            ok=False,
            payload={"error": "wire"},
        ),
        response_id="blob",
    )
    meta = dict(e2a.metadata or {})
    meta[E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY] = legacy
    e2a.metadata = meta
    wire = e2a.to_dict()
    back = parse_agent_server_wire_unary(wire)
    assert back.ok is True
    assert back.payload == {"recovered": True}


def test_inverse_raises_for_chunk_shape_on_unary_parser() -> None:
    chunk_wire = encode_agent_chunk_for_wire(
        AgentResponseChunk(
            request_id="u",
            channel_id="c",
            payload={"content": "x"},
            is_complete=False,
        ),
        response_id="u",
        sequence=0,
    )
    with pytest.raises(ValueError):
        parse_agent_server_wire_unary(chunk_wire)


# ---------------------------------------------------------------------------
# SDD-0010 — wire truncation keeps budget / token_count / child meta
# ---------------------------------------------------------------------------

def test_snapshot_keep_keys_include_budget() -> None:
    from jiuwenswarm.server.wire_truncate import (
        _WORKFLOW_SNAPSHOT_KEEP_KEYS,
        _WORKFLOW_LIST_SUMMARY_KEEP_KEYS,
    )

    assert "budget" in _WORKFLOW_SNAPSHOT_KEEP_KEYS
    assert "budget" in _WORKFLOW_LIST_SUMMARY_KEEP_KEYS


def test_collapse_agent_keeps_token_count() -> None:
    from jiuwenswarm.server.wire_truncate import _workflow_agent_for_collapse

    agent = {"id": "k1", "name": "analyst", "status": "completed", "kind": "agent",
             "token_count": 12700, "outcome": "ok"}
    out = _workflow_agent_for_collapse(agent)
    assert out.get("token_count") == 12700


def test_collapse_phase_keeps_child_meta() -> None:
    from jiuwenswarm.server.wire_truncate import _collapse_oversized_workflow_snapshot_item

    item = {
        "id": "wf_1", "name": "onboarding", "status": "running",
        "agent_count": 1, "completed_agent_count": 0,
        "started_at": "2026-08-01T10:00:00+08:00", "token_count": 12700,
        "budget": {"total": 5, "spent": 5, "remaining": 0, "scope": "leader", "exhausted": True},
        "phases": [{
            "id": "p1", "name": "▸ intro #0", "status": "running",
            "agent_count": 1, "completed_agent_count": 0,
            "phase_type": "child", "nested_phase": "▸ intro #0",
            "parent_phase": "review", "agents": [],
        }],
    }
    out = _collapse_oversized_workflow_snapshot_item(item)
    assert out["budget"]["exhausted"] is True
    assert out["token_count"] == 12700
    ph = out["phases"][0]
    assert ph.get("phase_type") == "child"
    assert ph.get("nested_phase") == "▸ intro #0"
    assert ph.get("parent_phase") == "review"
