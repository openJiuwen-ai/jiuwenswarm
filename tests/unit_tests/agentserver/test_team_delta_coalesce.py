# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""团队流增量按 1 秒合并，避免一块一帧灌满本地连接。"""

from __future__ import annotations

import jiuwenswarm.server.runtime.agent_adapter.team_helpers as team_helpers


def _clock(monkeypatch, start: float = 0.0):
    state = {"now": start}
    monkeypatch.setattr(team_helpers.time, "monotonic", lambda: state["now"])
    return state


async def test_first_reasoning_goes_out_and_later_text_waits(monkeypatch):
    clock = _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    first = {"event_type": "chat.reasoning", "role": "leader", "content": "你"}
    await coalescer.submit(None, "s1", first)
    clock["now"] = 0.4
    await coalescer.submit(
        None, "s1", {"event_type": "chat.reasoning", "role": "leader", "content": "好"}
    )
    assert [item["content"] for item in sent] == ["你"]
    clock["now"] = 1.0
    await coalescer.submit(
        None, "s1", {"event_type": "chat.reasoning", "role": "leader", "content": "啊"}
    )
    assert sent[-1]["content"] == "好啊"


async def test_members_do_not_share_a_bucket(monkeypatch):
    _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.reasoning", "role": "leader", "member_name": "甲", "content": "甲"},
    )
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.reasoning", "role": "teammate", "member_name": "乙", "content": "乙"},
    )
    assert [item["content"] for item in sent] == ["甲", "乙"]


async def test_tool_event_flushes_pending_text_first(monkeypatch):
    clock = _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    await coalescer.submit(
        None, "s1", {"event_type": "chat.reasoning", "role": "leader", "content": "先"}
    )
    clock["now"] = 0.2
    await coalescer.submit(
        None, "s1", {"event_type": "chat.reasoning", "role": "leader", "content": "说"}
    )
    await coalescer.submit(
        None, "s1", {"event_type": "chat.tool_result", "role": "leader", "content": "工具"}
    )
    assert [item["event_type"] for item in sent] == [
        "chat.reasoning",
        "chat.reasoning",
        "chat.tool_result",
    ]
    assert sent[1]["content"] == "说"
