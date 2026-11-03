# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for anchoring assistant nodes on the model call that wrote them."""

from __future__ import annotations

import json
from typing import Any

from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode

from jiuwenswarm.diagnosis.node_anchor import anchor_assistant_nodes
from tests.unit_tests.diagnosis.diagnosis_fixtures import TRACE_A, span_id


def _chat_span(index: int, *, text: str | None = None, call_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if text is not None:
        message["content"] = text
    if call_id is not None:
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
    return {
        "traceId": TRACE_A,
        "spanId": span_id(index),
        "name": "chat model",
        "startTimeUnixNano": str(index * 1000),
        "endTimeUnixNano": str(index * 1000 + 500),
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.output.messages", "value": {"stringValue": json.dumps([message])}},
        ],
    }


def _commit_span(index: int) -> dict[str, Any]:
    return {
        "traceId": TRACE_A,
        "spanId": span_id(index),
        "name": "context.window.commit",
        "startTimeUnixNano": str(index * 1000),
        "endTimeUnixNano": str(index * 1000),
        "attributes": [],
    }


def _node(
    node_id: str,
    *,
    role: str = "assistant",
    source_kind: str = "msg",
    span: int = 1,
    content: str | None = None,
    call_id: str | None = None,
) -> TrajectoryNode:
    tool_calls = [{"id": call_id, "type": "function", "function": {"name": "bash"}}] if call_id else []
    return TrajectoryNode(
        node_id=node_id,
        subagent_key="main",
        display_name="Main",
        kind="main_agent",
        role=role,
        content=content,
        tool_calls=tool_calls,
        trace_id=TRACE_A,
        span_id=span_id(span),
        source_kind=source_kind,
    )


def _snapshot(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": "s", "subjects": [{"subject_id": "main", "records": list(spans)}]}


def _span_of(nodes: dict[str, list[TrajectoryNode]], node_id: str) -> str | None:
    return next(node.span_id for node in nodes["main"] if node.node_id == node_id)


def test_assistant_message_moves_from_the_next_commit_to_the_call_that_wrote_it() -> None:
    snapshot = _snapshot(
        _chat_span(1, text="running it", call_id="call-1"),
        _chat_span(3, text="done"),
        _commit_span(4),
    )
    nodes = {"main": [_node("main:0", span=4, content="running it", call_id="call-1")]}

    anchored = anchor_assistant_nodes(nodes, snapshot)

    assert _span_of(anchored, "main:0") == span_id(1)


def test_final_reply_is_anchored_on_the_first_call_that_wrote_the_same_text() -> None:
    snapshot = _snapshot(
        _chat_span(1, text="all done"),
        _chat_span(3, text="all done"),
        _commit_span(4),
    )
    nodes = {"main": [_node("main:0", span=4, content="all done")]}

    anchored = anchor_assistant_nodes(nodes, snapshot)

    assert _span_of(anchored, "main:0") == span_id(1)


def test_turn_output_is_anchored_on_the_last_reply_it_contains() -> None:
    snapshot = _snapshot(
        _chat_span(1, text="step one done."),
        _chat_span(3, text="all finished."),
    )
    nodes = {"main": [_node("main:0", source_kind="turn", span=9, content="step one done.all finished.")]}

    anchored = anchor_assistant_nodes(nodes, snapshot)

    assert _span_of(anchored, "main:0") == span_id(3)


def test_other_nodes_and_unknown_writers_keep_their_span() -> None:
    snapshot = _snapshot(_chat_span(1, text="hello"))
    nodes = {
        "main": [
            _node("main:0", role="user", span=7, content="hello"),
            _node("main:1", role="tool", source_kind="tool", span=8, content="hello"),
            _node("main:2", source_kind="llm", span=6, content="hello"),
            _node("main:3", span=5, content="never written by a call"),
            _node("main:4", span=5, content=None),
        ],
    }

    anchored = anchor_assistant_nodes(nodes, snapshot)

    assert [node.span_id for node in anchored["main"]] == [
        span_id(7),
        span_id(8),
        span_id(6),
        span_id(5),
        span_id(5),
    ]


def test_anchoring_changes_only_the_span_identity() -> None:
    snapshot = _snapshot(_chat_span(1, text="hello"))
    original = _node("main:0", span=4, content="hello")

    anchored = anchor_assistant_nodes({"main": [original]}, snapshot)["main"][0]

    assert anchored.span_id == span_id(1)
    assert anchored.trace_id == original.trace_id
    assert (anchored.node_id, anchored.content, anchored.start_ts_ns) == (
        original.node_id,
        original.content,
        original.start_ts_ns,
    )
    assert original.span_id == span_id(4)
