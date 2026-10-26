# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Regression: chat.final body must not triple on delta + final + post-tool \\n\\n."""

from __future__ import annotations

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_drain_final_clears_content_after_reroute() -> None:
    parsed = {"event_type": "chat.final", "content": "已为你添加待办事项"}
    deltas = JiuWenSwarmDeepAdapter._drain_final_content_to_deltas(
        parsed,
        segment_streamed_text="",
        chunk_payload=None,
    )
    assert len(deltas) == 1
    assert deltas[0]["event_type"] == "chat.delta"
    assert deltas[0]["content"] == "已为你添加待办事项"
    assert parsed["content"] == ""


def test_drain_final_clears_when_already_streamed_without_reroute() -> None:
    body = "已为你添加待办事项"
    parsed = {"event_type": "chat.final", "content": body}
    deltas = JiuWenSwarmDeepAdapter._drain_final_content_to_deltas(
        parsed,
        segment_streamed_text=body,
        chunk_payload=None,
    )
    assert deltas == []
    assert parsed["content"] == ""


def test_drain_final_skips_when_substantial_stream_already_present() -> None:
    body = "已为你添加待办事项：完整确认正文会比较长一些"
    # Partial but substantial stream (simulates llm_output before answer).
    streamed = body[: max(8, len(body) // 2)]
    parsed = {"event_type": "chat.final", "content": body}
    deltas = JiuWenSwarmDeepAdapter._drain_final_content_to_deltas(
        parsed,
        segment_streamed_text=streamed,
        chunk_payload=None,
    )
    assert deltas == []
    assert parsed["content"] == ""


def test_drain_final_noop_for_non_final() -> None:
    parsed = {"event_type": "chat.delta", "content": "x"}
    deltas = JiuWenSwarmDeepAdapter._drain_final_content_to_deltas(
        parsed,
        segment_streamed_text="",
        chunk_payload=None,
    )
    assert deltas == []
    assert parsed["content"] == "x"
