# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deep 外层流增量按 1 秒合并，且不把不同来源拼进同一帧。"""

from __future__ import annotations

import jiuwenswarm.server.handlers.stream_delta_coalesce as coalesce
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.handlers.stream_delta_coalesce import StreamDeltaCoalescer


def _clock(monkeypatch, start: float = 0.0):
    state = {"now": start}
    monkeypatch.setattr(coalesce.time, "monotonic", lambda: state["now"])
    return state


def _chunk(event_type: str, content: str, **payload) -> AgentResponseChunk:
    body = {"event_type": event_type, "content": content}
    body.update(payload)
    return AgentResponseChunk(request_id="r1", channel_id="test-channel", payload=body)


def _texts(chunks: list[AgentResponseChunk]) -> list[str]:
    return [str((item.payload or {}).get("content")) for item in chunks]


def test_first_reasoning_goes_out_and_later_text_waits(monkeypatch):
    clock = _clock(monkeypatch)
    merger = StreamDeltaCoalescer(enabled=True)
    sent = merger.push(_chunk("chat.reasoning", "你"))
    clock["now"] = 0.4
    sent.extend(merger.push(_chunk("chat.reasoning", "好")))
    assert _texts(sent) == ["你"]
    clock["now"] = 1.0
    sent.extend(merger.push(_chunk("chat.reasoning", "啊")))
    assert _texts(sent) == ["你", "好啊"]


def test_sources_do_not_share_a_bucket(monkeypatch):
    clock = _clock(monkeypatch)
    merger = StreamDeltaCoalescer(enabled=True)
    sent = merger.push(_chunk("chat.reasoning", "甲", stream_source_id="page-1"))
    sent.extend(merger.push(_chunk("chat.reasoning", "乙", stream_source_id="page-2")))
    clock["now"] = 0.2
    sent.extend(merger.push(_chunk("chat.reasoning", "1", stream_source_id="page-1")))
    clock["now"] = 1.0
    sent.extend(merger.push(_chunk("chat.reasoning", "2", stream_source_id="page-2")))
    assert _texts(sent) == ["甲", "乙", "1", "2"]
    assert [item.payload["stream_source_id"] for item in sent] == [
        "page-1", "page-2", "page-1", "page-2",
    ]
    assert merger.flush() == []


def test_task_event_flushes_pending_text_and_next_token_is_immediate(monkeypatch):
    clock = _clock(monkeypatch)
    merger = StreamDeltaCoalescer(enabled=True)
    sent = merger.push(_chunk("chat.reasoning", "先"))
    clock["now"] = 0.2
    sent.extend(merger.push(_chunk("chat.reasoning", "说")))
    sent.extend(merger.push(_chunk("task.update", "")))
    assert [item.payload["event_type"] for item in sent] == [
        "chat.reasoning",
        "chat.reasoning",
        "task.update",
    ]
    assert sent[1].payload["content"] == "说"
    clock["now"] = 0.3
    sent.extend(merger.push(_chunk("chat.reasoning", "下")))
    assert sent[-1].payload["content"] == "下"


def test_disabled_coalescer_does_not_hold(monkeypatch):
    _clock(monkeypatch)
    merger = StreamDeltaCoalescer(enabled=False)
    first = merger.push(_chunk("chat.reasoning", "你"))
    second = merger.push(_chunk("chat.reasoning", "好"))
    assert _texts(first + second) == ["你", "好"]
    assert merger.flush() == []


def test_stream_end_flushes_tail(monkeypatch):
    clock = _clock(monkeypatch)
    merger = StreamDeltaCoalescer(enabled=True)
    merger.push(_chunk("chat.delta", "已"))
    clock["now"] = 0.2
    assert merger.push(_chunk("chat.delta", "发")) == []
    assert _texts(merger.flush()) == ["发"]
