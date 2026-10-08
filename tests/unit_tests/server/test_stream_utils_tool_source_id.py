# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""stream_source_id propagation for tool frames parsed by stream_utils."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.utils.stream_utils import _parse_typed_chunk


def _chunk(chunk_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=chunk_type, payload=payload)


def test_tool_call_propagates_source_id_without_task_id() -> None:
    parsed = _parse_typed_chunk(
        _chunk(
            "tool_call",
            {
                "tool_call": {"id": "c1", "name": "web_search", "arguments": {"q": "x"}},
                "stream_source_id": "sub-1",
                "task_id": "t-child",
            },
        ),
        False,
    )

    assert parsed["event_type"] == "chat.tool_call"
    assert parsed["stream_source_id"] == "sub-1"
    assert "task_id" not in parsed
    assert parsed["tool_call"]["name"] == "web_search"


def test_tool_result_propagates_source_id() -> None:
    parsed = _parse_typed_chunk(
        _chunk(
            "tool_result",
            {
                "tool_result": {"tool_call_id": "c1", "tool_name": "web_search", "result": "ok"},
                "stream_source_id": "sub-1",
            },
        ),
        False,
    )

    assert parsed["event_type"] == "chat.tool_result"
    assert parsed["stream_source_id"] == "sub-1"
    assert parsed["tool_call_id"] == "c1"


def test_tool_update_propagates_source_id() -> None:
    parsed = _parse_typed_chunk(
        _chunk("tool_update", {"content": "working", "stream_source_id": "sub-1"}),
        False,
    )

    assert parsed["event_type"] == "chat.tool_update"
    assert parsed["stream_source_id"] == "sub-1"


def test_tool_frames_without_source_id_stay_unchanged() -> None:
    parsed = _parse_typed_chunk(
        _chunk("tool_call", {"tool_call": {"id": "c1", "name": "web_search"}}),
        False,
    )

    assert "stream_source_id" not in parsed
