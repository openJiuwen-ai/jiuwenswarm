# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for parent-owned subagent history persistence."""

from __future__ import annotations

from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.channel_manager.web.web_ws_transport import (
    _WEB_FULL_PAYLOAD_EVENT_TYPES,
)
from jiuwenswarm.server.runtime.session import session_history, session_metadata


def test_subagent_history_writes_use_dedicated_child_bucket(monkeypatch) -> None:
    persisted: list[tuple[str, str, list[dict], str | None]] = []
    metadata_updates: list[dict] = []
    monkeypatch.setattr(
        session_history,
        "_batch_write_subagent_items",
        lambda sid, child_id, items, root: persisted.append((sid, child_id, items, root)),
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
    sid, child_id, items, _root = persisted[0]
    assert sid == "parent-session"
    assert child_id == "subagent-1"
    assert len(items) == 1
    assert items[0]["session_id"] == "parent-session"
    assert items[0]["subagent_id"] == "subagent-1"
    assert items[0]["mode"] == "subagent"
    assert metadata_updates == []


def test_subagent_rows_go_under_parent_subagents_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_history.append_history_record(
        session_id="parent1",
        request_id="r-parent",
        channel_id="web",
        role="user",
        content="hello",
        timestamp=1.0,
    )
    session_history.append_history_record(
        session_id="parent1",
        subagent_id="sa-explore-1",
        request_id="sa-explore-1:1",
        channel_id="subagent",
        role="assistant",
        content="child note",
        timestamp=2.0,
        event_type="chat.delta",
        mode="subagent",
        extra={"parent_session_id": "parent1"},
    )

    parent_rows = session_history.load_history_records("parent1")
    child_rows = session_history.load_history_records(
        "parent1",
        subagent_id="sa-explore-1",
    )
    assert [row.get("content") for row in parent_rows] == ["hello"]
    assert any(row.get("content") == "child note" for row in child_rows)
    child_path, error = session_history.resolve_subagent_history_path(
        "parent1",
        "sa-explore-1",
        create=False,
    )
    assert error is None
    assert child_path is not None
    assert child_path.exists()
    assert "subagents" in child_path.parts
    assert not session_history.history_exists("parent1", subagent_id="../escape")


def test_empty_subagent_activity_is_persistable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_history.append_history_record(
        session_id="parent1",
        subagent_id="sa-1",
        request_id="sa-1:activity:turn:1",
        channel_id="subagent",
        role="assistant",
        content="",
        timestamp=3.0,
        event_type="chat.subagent_activity",
        extra={"subagent_activity": {"subagent_id": "sa-1", "summary": ""}},
        mode="subagent",
    )
    rows = session_history.load_history_records("parent1", subagent_id="sa-1")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "chat.subagent_activity"


def test_web_allowlist_includes_subagent_activity() -> None:
    assert EventType.CHAT_SUBAGENT_ACTIVITY.value == "chat.subagent_activity"
    assert "chat.subagent_activity" in _WEB_FULL_PAYLOAD_EVENT_TYPES
    assert "task.start" in _WEB_FULL_PAYLOAD_EVENT_TYPES
