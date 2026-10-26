# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming text chunks must preserve formatting whitespace."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@pytest.mark.parametrize(
    ("chunk_type", "content", "event_type"),
    [
        ("llm_output", " ", "chat.delta"),
        ("llm_output", "\n", "chat.delta"),
        ("content_chunk", "\n\n", "chat.delta"),
        ("llm_reasoning", " ", "chat.reasoning"),
    ],
)
def test_parse_stream_chunk_preserves_whitespace(
    chunk_type: str,
    content: str,
    event_type: str,
) -> None:
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        SimpleNamespace(type=chunk_type, payload={"content": content})
    )

    assert parsed == {"event_type": event_type, "content": content}


def test_stream_text_payload_skips_only_absent_or_empty_content() -> None:
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", None) is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", "") is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload(
        "chat.delta", " hello"
    ) == {"event_type": "chat.delta", "content": " hello"}
