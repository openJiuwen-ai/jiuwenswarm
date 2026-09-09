# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for parent-owned subagent history persistence."""

from __future__ import annotations

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.session import session_history, session_metadata


def test_subagent_history_writes_use_dedicated_child_bucket(monkeypatch) -> None:
    persisted = []
    monkeypatch.setattr(
        interface_deep,
        "append_history_record",
        lambda **kwargs: persisted.append(kwargs),
    )
    projection = {
        "parent_session_id": "parent-session",
        "subagent_id": "subagent-1",
        "task_id": "task-1",
        "seq": 1,
        "role": "assistant",
        "content": "result",
        "summary": "working",
        "event_type": "chat.final",
    }

    JiuWenSwarmDeepAdapter._persist_subagent_transcript_message(projection)
    JiuWenSwarmDeepAdapter._persist_subagent_activity(projection)
    JiuWenSwarmDeepAdapter._persist_subagent_roster_history(
        projection,
        {"description": "worker"},
    )

    assert len(persisted) == 3
    assert all(item["session_id"] == "parent-session" for item in persisted)
    assert all(item["subagent_id"] == "subagent-1" for item in persisted)
    assert all(item["mode"] == "subagent" for item in persisted)


def test_subagent_user_history_does_not_replace_parent_delivery_context(
    monkeypatch,
) -> None:
    enqueued = []
    delivery_updates = []
    monkeypatch.setattr(
        session_history,
        "_enqueue_history_item",
        lambda session_id, item, *, subagent_id=None: enqueued.append(
            (session_id, item, subagent_id)
        ),
    )
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(
        session_metadata,
        "set_session_delivery_context",
        lambda **kwargs: delivery_updates.append(kwargs),
    )

    session_history.append_history_record(
        session_id="parent-session",
        subagent_id="subagent-1",
        request_id="subagent-1:1",
        channel_id="subagent",
        role="user",
        content="child task",
        timestamp=1.0,
        mode="subagent",
    )
    session_history.append_history_record(
        session_id="parent-session",
        request_id="parent-1",
        channel_id="web",
        role="user",
        content="parent task",
        timestamp=2.0,
        mode="deep",
    )

    assert enqueued[0][2] == "subagent-1"
    assert delivery_updates == [
        {
            "session_id": "parent-session",
            "channel_id": "web",
            "source_request_id": "parent-1",
            "route_metadata": None,
        }
    ]
