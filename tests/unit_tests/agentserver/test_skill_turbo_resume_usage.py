# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HITL resume must surface SkillTurbo llm_usage as history usage events."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_extract_usage_from_skill_turbo_llm_usage_payload() -> None:
    payload = {
        "event_type": "chat.llm_usage",
        "usage_metadata": {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "cache_tokens": 200,
        },
        "plan_name": "p4_content_plan",
        "task_id": "task_abc",
    }
    meta = JiuWenSwarmDeepAdapter._extract_usage_metadata(payload)
    assert meta == payload["usage_metadata"]


def test_extract_usage_ignores_non_usage_events() -> None:
    payload = {
        "event_type": "chat.tool_call",
        "usage_metadata": {"input_tokens": 1},
        "tool_call": {"name": "write_file"},
    }
    assert JiuWenSwarmDeepAdapter._extract_usage_metadata(payload) is None


def test_rewrite_skill_turbo_usage_chunk_to_usage_metadata() -> None:
    chunk = AgentResponseChunk(
        request_id="r1",
        channel_id="officeclaw",
        payload={
            "event_type": "chat.llm_usage",
            "usage_metadata": {
                "input_tokens": 2200,
                "output_tokens": 80,
                "total_tokens": 2280,
                "cache_tokens": 400,
            },
            "plan_name": "p6_page_gen",
        },
        is_complete=False,
    )
    rewritten, meta = JiuWenSwarmDeepAdapter._rewrite_skill_turbo_usage_chunk(
        chunk, session_id="sess-1"
    )
    assert meta is not None
    assert meta["input_tokens"] == 2200
    assert rewritten is not None
    assert rewritten.payload["event_type"] == "chat.usage_metadata"
    assert rewritten.payload["session_id"] == "sess-1"
    assert rewritten.payload["metadata"]["plan_name"] == "p6_page_gen"
    assert rewritten.payload["metadata"]["usage_metadata"]["output_tokens"] == 80


def test_rewrite_passes_through_existing_usage_metadata() -> None:
    chunk = AgentResponseChunk(
        request_id="r1",
        channel_id="c1",
        payload={
            "event_type": "chat.usage_metadata",
            "metadata": {"usage_metadata": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}},
            "session_id": "sess-1",
        },
    )
    rewritten, meta = JiuWenSwarmDeepAdapter._rewrite_skill_turbo_usage_chunk(
        chunk, session_id="sess-1"
    )
    assert rewritten is chunk
    assert meta["total_tokens"] == 4


def test_resume_usage_loop_accumulates_and_keeps_other_events() -> None:
    chunks = [
        AgentResponseChunk(
            request_id="r1",
            channel_id="c1",
            payload={
                "event_type": "chat.llm_usage",
                "usage_metadata": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "cache_tokens": 10,
                },
                "plan_name": "p4_content_plan",
            },
        ),
        AgentResponseChunk(
            request_id="r1",
            channel_id="c1",
            payload={
                "event_type": "chat.llm_usage",
                "usage_metadata": {
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "total_tokens": 230,
                    "cache_tokens": 50,
                },
                "plan_name": "p6_page_gen",
            },
        ),
        AgentResponseChunk(
            request_id="r1",
            channel_id="c1",
            payload={"event_type": "chat.tool_call", "tool_call": {"name": "write_file"}},
        ),
        AgentResponseChunk(request_id="r1", channel_id="c1", payload=None, is_complete=True),
    ]
    acc = JiuWenSwarmDeepAdapter._new_usage_accumulator()
    out: list[AgentResponseChunk] = []
    for chunk in chunks:
        rewritten, usage_meta = JiuWenSwarmDeepAdapter._rewrite_skill_turbo_usage_chunk(
            chunk, session_id="sess-1"
        )
        if rewritten is not None and usage_meta is not None:
            JiuWenSwarmDeepAdapter._accumulate_usage(acc, usage_meta)
            out.append(rewritten)
            continue
        out.append(chunk)

    assert acc["input_tokens"] == 300
    assert acc["output_tokens"] == 50
    assert acc["total_tokens"] == 350
    assert acc["cache_tokens"] == 60
    assert [c.payload.get("event_type") if c.payload else None for c in out] == [
        "chat.usage_metadata",
        "chat.usage_metadata",
        "chat.tool_call",
        None,
    ]
    assert out[-1].is_complete is True


def test_deepresearch_sdk_usage_is_accounted_once_from_terminal_tool_result(
    monkeypatch,
) -> None:
    usage = {
        "input_tokens": 500,
        "output_tokens": 80,
        "total_tokens": 580,
        "llm_call_count": 4,
        "agent_name_token_usage": [
            {
                "agent_name": "reporter",
                "input_tokens": 500,
                "output_tokens": 80,
                "total_tokens": 580,
                "llm_call_count": 4,
            }
        ],
    }
    payload = {
        "tool_result": {
            "tool_name": "deepresearch_execute",
            "tool_call_id": "call-1",
            "raw_output": {
                "schema_version": "openjiuwen.deepresearch.execute.v1",
                "kind": "completed",
                "state": {"conversation_id": "conversation-1"},
                "workflow_llm_token_usage": usage,
            },
        }
    }
    recorded: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep."
        "record_deepresearch_sdk_token_usage",
        lambda request_id, usage_id, value: recorded.append(
            (request_id, usage_id, value)
        ),
    )
    accumulator = JiuWenSwarmDeepAdapter._new_usage_accumulator()
    accounted: set[str] = set()

    first = JiuWenSwarmDeepAdapter._account_deepresearch_sdk_usage(
        payload,
        request_id="request-1",
        usage_accumulator=accumulator,
        accounted_usage_ids=accounted,
    )
    second = JiuWenSwarmDeepAdapter._account_deepresearch_sdk_usage(
        payload,
        request_id="request-1",
        usage_accumulator=accumulator,
        accounted_usage_ids=accounted,
    )

    assert first is True
    assert second is False
    assert accumulator["input_tokens"] == 500
    assert accumulator["output_tokens"] == 80
    assert accumulator["total_tokens"] == 580
    assert recorded == [("request-1", "conversation-1", usage)]


def test_deepresearch_sdk_usage_rejects_invalid_or_nonterminal_payloads() -> None:
    invalid_usage = {
        "input_tokens": -1,
        "output_tokens": 2,
        "total_tokens": 1,
        "llm_call_count": 1,
        "agent_name_token_usage": [],
    }

    for kind, tool_name in (
        ("completed", "other_tool"),
        ("interaction", "deepresearch_execute"),
        ("completed", "deepresearch_execute"),
    ):
        usage = invalid_usage if tool_name == "deepresearch_execute" else {
            **invalid_usage,
            "input_tokens": 1,
        }
        payload = {
            "tool_result": {
                "tool_name": tool_name,
                "tool_call_id": "call-1",
                "raw_output": {
                    "kind": kind,
                    "workflow_llm_token_usage": usage,
                },
            }
        }
        assert JiuWenSwarmDeepAdapter._extract_deepresearch_sdk_usage(payload) is None
