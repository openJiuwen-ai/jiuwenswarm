# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parent-turn bookkeeping ignores frames mirrored from a nested stream."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _is_foreign_stream_source as facade_is_foreign,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _is_foreign_stream_source as adapter_is_foreign,
)


@pytest.mark.parametrize("predicate", [facade_is_foreign, adapter_is_foreign])
def test_foreign_source_detection(predicate) -> None:
    assert predicate({"stream_source_id": "sub-1"}) is True
    assert predicate({"stream_source_id": "main"}) is False
    assert predicate({"stream_source_id": ""}) is False
    assert predicate({}) is False
    assert predicate(None) is False


class _Logger:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def feed(self, chunk: object) -> None:
        self.sources.append("main")


def test_note_chat_payload_skips_foreign_tool_call() -> None:
    """A mirrored child tool_call must not clear the parent's streamed flags."""
    state = {"has_streamed_content": True, "segment_streamed_text": "父正文"}

    def note(payload: dict) -> dict:
        if adapter_is_foreign(payload):
            return payload
        if payload.get("event_type") == "chat.tool_call":
            state["segment_streamed_text"] = ""
            state["has_streamed_content"] = False
        return payload

    note({"event_type": "chat.tool_call", "stream_source_id": "sub-1", "tool_call": {}})

    assert state == {"has_streamed_content": True, "segment_streamed_text": "父正文"}


def test_debug_feed_skips_mirrored_chunk() -> None:
    """The parent run dump must not re-record chunks mirrored from a child."""
    debug_logger = _Logger()
    chunk = SimpleNamespace(type="tool_call", payload={"stream_source_id": "sub-1"})

    if not adapter_is_foreign(getattr(chunk, "payload", None)):
        debug_logger.feed(chunk)

    assert debug_logger.sources == []


def test_adapter_parser_propagates_source_id_on_tool_call(caplog) -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    chunk = SimpleNamespace(
        type="tool_call",
        payload={
            "tool_call": {"id": "c1", "name": "web_search"},
            "stream_source_id": "sub-1",
            "task_id": "t-child",
        },
    )

    parsed = adapter._parse_stream_chunk(chunk)

    assert parsed is not None, caplog.text
    assert parsed["event_type"] == "chat.tool_call"
    assert parsed["stream_source_id"] == "sub-1"
    assert "task_id" not in parsed
