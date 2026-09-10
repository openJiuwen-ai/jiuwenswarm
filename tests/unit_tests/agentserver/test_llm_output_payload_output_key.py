# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""llm_output must accept openjiuwen payload.output (same as llm_reasoning)."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def test_parse_stream_chunk_llm_output_from_output_key() -> None:
    chunk = SimpleNamespace(
        type="llm_output",
        payload={"output": "搜索到了杭州气温数据来源。", "result_type": "answer"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed == {
        "event_type": "chat.delta",
        "content": "搜索到了杭州气温数据来源。",
    }


def test_parse_stream_chunk_llm_output_from_content_key() -> None:
    chunk = SimpleNamespace(
        type="llm_output",
        payload={"content": "SkillTurbo body"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed == {"event_type": "chat.delta", "content": "SkillTurbo body"}


def test_parse_stream_chunk_llm_reasoning_still_accepts_output() -> None:
    chunk = SimpleNamespace(
        type="llm_reasoning",
        payload={"output": "Let me plan the steps."},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed == {
        "event_type": "chat.reasoning",
        "content": "Let me plan the steps.",
    }
