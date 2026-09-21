# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for parent-owned subagent history persistence."""

from __future__ import annotations

from jiuwenswarm.server.runtime.session import session_history, session_metadata


def test_subagent_history_writes_use_dedicated_child_bucket(monkeypatch) -> None:
    persisted: list[dict] = []
    metadata_updates: list[dict] = []
    monkeypatch.setattr(
        session_history,
        "_ensure_flush_thread_started",
        lambda: None,
    )
    monkeypatch.setattr(session_history, "_flush_on_request_switch", lambda *_args: None)
    monkeypatch.setattr(
        session_history,
        "_route_event",
        lambda _sid, item, _event_type, _root: persisted.append(item),
    )
    monkeypatch.setattr(
        session_metadata,
        "update_session_metadata",
        lambda **kwargs: metadata_updates.append(kwargs),
    )

    session_history.append_history_record(
        session_id="parent-session",
        request_id="request-1",
        channel_id="web",
        role="assistant",
        content="result",
        timestamp=1.0,
        event_type="chat.final",
        task_id="task-1",
        subagent_id="subagent-1",
        mode="subagent",
    )

    assert len(persisted) == 1
    assert persisted[0]["session_id"] == "parent-session"
    assert persisted[0]["subagent_id"] == "subagent-1"
    assert persisted[0]["mode"] == "subagent"
    assert metadata_updates[0]["session_id"] == "parent-session"
    assert metadata_updates[0]["mode"] is None
