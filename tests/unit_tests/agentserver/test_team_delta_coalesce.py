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


async def test_concurrent_sources_do_not_share_a_bucket(monkeypatch):
    clock = _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.reasoning",
            "role": "leader",
            "content": "甲",
            "stream_source_id": "page-1",
        },
    )
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.reasoning",
            "role": "leader",
            "content": "乙",
            "stream_source_id": "page-2",
        },
    )
    clock["now"] = 0.2
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.reasoning",
            "role": "leader",
            "content": "1",
            "stream_source_id": "page-1",
        },
    )
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.reasoning",
            "role": "leader",
            "content": "2",
            "stream_source_id": "page-2",
        },
    )
    clock["now"] = 1.0
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.reasoning",
            "role": "leader",
            "content": "3",
            "stream_source_id": "page-1",
        },
    )
    assert [item["content"] for item in sent] == ["甲", "乙", "2", "13"]
    assert [item["stream_source_id"] for item in sent] == [
        "page-1", "page-2", "page-2", "page-1",
    ]


async def test_other_speaker_expired_text_flushes_on_next_submit(monkeypatch):
    clock = _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.delta", "role": "teammate", "member_name": "甲", "content": "甲"},
    )
    clock["now"] = 0.2
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.delta", "role": "teammate", "member_name": "甲", "content": "尾"},
    )
    clock["now"] = 1.2
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.delta", "role": "teammate", "member_name": "乙", "content": "乙"},
    )
    assert [item["content"] for item in sent] == ["甲", "尾", "乙"]


async def test_usage_flushes_that_speaker_before_itself(monkeypatch):
    clock = _clock(monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )
    coalescer = team_helpers._TeamDeltaCoalescer()
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.delta", "role": "leader", "member_name": "甲", "content": "先"},
    )
    clock["now"] = 0.2
    await coalescer.submit(
        None, "s1",
        {"event_type": "chat.delta", "role": "leader", "member_name": "甲", "content": "说"},
    )
    await coalescer.submit(
        None, "s1",
        {
            "event_type": "chat.usage_metadata",
            "role": "leader",
            "member_name": "甲",
            "metadata": {"usage_metadata": {"total_tokens": 1}},
        },
    )
    assert [item["event_type"] for item in sent] == [
        "chat.delta",
        "chat.delta",
        "chat.usage_metadata",
    ]
    assert sent[1]["content"] == "说"


async def test_team_completed_flushes_pending_text_before_complete(monkeypatch):
    from types import SimpleNamespace

    sent: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda _c, _s, event: sent.append(dict(event))
    )

    class _Manager:
        @staticmethod
        def owns_stream_task(session_id, task) -> bool:
            return True

        @staticmethod
        def reset_seen_team_events(session_id: str) -> None:
            return None

        @staticmethod
        def reset_round_complete(session_id: str) -> None:
            return None

        @staticmethod
        def reset_workflow_completed(session_id: str) -> None:
            return None

        @staticmethod
        def claim_round_complete(session_id: str) -> bool:
            return True

        @staticmethod
        def get_monitor_handler(session_id: str):
            return None

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            return None

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            return None

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            return None

        @staticmethod
        def clear_session_initialized(session_id: str) -> None:
            return None

    async def _stream(**kwargs):
        yield SimpleNamespace(
            type="llm_reasoning",
            payload={"content": "你"},
            role="leader",
        )
        yield SimpleNamespace(
            type="llm_reasoning",
            payload={"content": "好"},
            role="leader",
        )
        yield SimpleNamespace(
            type="message",
            payload={"event_type": "team.completed", "member_count": 1, "task_count": 0},
            role="leader",
        )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _Manager())
    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _stream)

    await team_helpers._consume_stream_with_query(
        None,
        "s1",
        SimpleNamespace(team_name="demo"),
        "q",
        round_id=1,
    )
    reasoning = [item for item in sent if item["event_type"] == "chat.reasoning"]
    assert [item["content"] for item in reasoning] == ["你", "好"]
    completes = [
        index for index, item in enumerate(sent)
        if item.get("event_type") == "chat.processing_status" and item.get("is_complete") is True
    ]
    assert len(completes) == 1
    assert sent.index(reasoning[1]) < completes[0]
