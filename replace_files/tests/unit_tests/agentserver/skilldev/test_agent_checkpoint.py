# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from jiuwenclaw.agentserver.skilldev.session_history.agent_checkpoint import (
    AGENT_RUNNER,
    build_agent_restore_payload,
    build_agent_session_summary,
    derive_agent_status,
    is_agent_checkpoint,
    merge_agent_checkpoint,
)
from jiuwenclaw.agentserver.skilldev.session_history.assembler import normalize_timeline
from jiuwenclaw.agentserver.skilldev.session_history.schema import SkillDevSessionEventRecord


def _event(seq: int, event_type: str, payload: dict | None = None) -> SkillDevSessionEventRecord:
    return SkillDevSessionEventRecord(
        seq=seq,
        timestamp="2026-05-23T08:00:00Z",
        source="assistant" if event_type.startswith("skilldev.") and "user" not in event_type else "user",
        event_type=event_type,
        payload=payload or {},
    )


def test_derive_agent_status_completed():
    events = [
        _event(1, "skilldev.user_start", {"query": "hello"}),
        _event(2, "skilldev.completed", {"task_id": "t1"}),
    ]
    assert derive_agent_status(events) == "completed"


def test_derive_agent_status_idle_after_agent_completed():
    events = [
        _event(1, "skilldev.user_start", {"query": "hello"}),
        _event(2, "skilldev.agent_completed", {"task_id": "t1"}),
    ]
    assert derive_agent_status(events) == "idle"


def test_derive_agent_status_pending_interaction_on_confirm():
    events = [
        _event(1, "skilldev.user_start", {"query": "hello"}),
        _event(
            2,
            "skilldev.confirm_request",
            {"confirm_type": "review", "task_id": "t1"},
        ),
    ]
    assert derive_agent_status(events) == "pending_interaction"


def test_derive_agent_status_pending_interaction_resolved():
    events = [
        _event(1, "skilldev.user_start", {"query": "hello"}),
        _event(
            2,
            "skilldev.confirm_request",
            {"confirm_type": "review", "task_id": "t1"},
        ),
        _event(
            3,
            "skilldev.confirm_resolved",
            {"confirm_type": "review", "action": "accept"},
        ),
    ]
    assert derive_agent_status(events) == "idle"


def test_build_agent_session_summary():
    checkpoint = {
        "runner": AGENT_RUNNER,
        "task_id": "task-1",
        "status": "idle",
        "created_at": "2026-05-23T08:00:00Z",
        "updated_at": "2026-05-23T08:05:00Z",
        "input": {"query": "做一个天气 skill", "skill_name": "weather"},
        "todos": [
            {"id": "1", "label": "a", "status": "completed"},
            {"id": "2", "label": "b", "status": "pending"},
        ],
    }
    summary = build_agent_session_summary("task-1", checkpoint)
    assert summary["runner"] == "agent"
    assert summary["status"] == "idle"
    assert summary["status_label"] == "等待继续"
    assert summary["title"] == "weather"
    assert summary["todo_progress"] == "1/2"
    assert summary["is_suspended"] is False


def test_build_agent_restore_payload_timeline():
    events = [
        _event(1, "skilldev.user_start", {"query": "q1"}),
        _event(2, "skilldev.agent_output", {"delta": "hi"}),
        _event(3, "skilldev.agent_completed", {"task_id": "t1"}),
    ]
    checkpoint = merge_agent_checkpoint(
        None,
        task_id="t1",
        events=events,
        task_workspace=Path("/nonexistent"),
    )
    timeline = normalize_timeline(events)
    restored = build_agent_restore_payload(
        task_id="t1",
        checkpoint=checkpoint,
        events=events,
        timeline_items=timeline,
    )
    assert restored["runner"] == "agent"
    assert restored["version"] == "2"
    assert restored["snapshot"]["status"] == "idle"
    assert restored["snapshot"]["query"] == "q1"
    assert len(restored["timeline_items"]) == 3
    assert is_agent_checkpoint(checkpoint)
