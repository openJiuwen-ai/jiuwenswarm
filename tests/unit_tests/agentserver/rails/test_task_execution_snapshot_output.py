# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Committed task snapshots retain reminder and consumer-output contracts."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TaskExecutionRail,
)
from jiuwenswarm.common.todo_output import TODO_SNAPSHOT_MODE


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
@pytest.mark.parametrize("include_persistent", [False, True])
@pytest.mark.parametrize("source", ["committed", "disk"])
async def test_task_update_projects_snapshot(tmp_path, monkeypatch, mode, include_persistent, source):
    snapshot = {
        "plan_id": "plan", "revision": 7,
        "todos": [
            {"id": "reminder", "content": "remind", "status": "pending",
             "meta_data": {"persistent": True}, "generation_token": "old"},
            {"id": "first", "content": "first", "status": "in_progress", "generation_token": "now"},
            {"id": "second", "content": "second", "status": "completed", "generation_token": "now"},
            {"id": "stale", "content": "stale", "status": "completed", "generation_token": "old"},
        ],
    }
    original = copy.deepcopy(snapshot)
    path = tmp_path / "todo.json"
    path.write_text(json.dumps(snapshot))
    rail = TaskExecutionRail()
    rail._todo_generation_token = "now"
    monkeypatch.setattr(rail, "_get_todo_workspace_path", lambda _sid: path)
    session = SimpleNamespace(get_session_id=lambda: "session", write_stream=AsyncMock())
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await rail._emit_task_update_event(
            session, "request", include_persistent=include_persistent,
            snapshot=snapshot if source == "committed" else None,
        )
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    payload = session.write_stream.call_args.args[0].payload
    if mode == "versioned":
        expected_ids = ["reminder", "first", "second", "stale"]
        assert payload["todo_meta"] == {"plan_id": "plan", "revision": 7}
    else:
        expected_ids = (["reminder"] if include_persistent else []) + ["first", "second"]
        assert "todo_meta" not in payload
    assert [t["task_id"] for t in payload["tasks"]] == expected_ids
    second = next(t for t in payload["tasks"] if t["task_id"] == "second")
    assert second["status"] == ("completed" if mode == "versioned" else "pending")
    assert payload["total_tasks"] == len(expected_ids)
    assert payload["parent_request_id"] == "request"
    assert snapshot == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
async def test_task_boundaries_do_not_leak_metadata_in_legacy(mode):
    rail = TaskExecutionRail()
    session = SimpleNamespace(write_stream=AsyncMock())
    task = {"content": "first", "todo_meta": {"plan_id": "plan", "revision": 7}}
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await rail._emit_task_start_event(session, "first", task, "request", source="todo")
        await rail._emit_task_complete_event(session, "first", task, status="succeeded")
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert session.write_stream.call_count == 2
    for call in session.write_stream.call_args_list:
        payload = call.args[0].payload
        assert ("todo_meta" in payload) == (mode == "versioned")
        assert payload["task_id"] == "todo:first"


@pytest.mark.asyncio
async def test_versioned_task_and_todo_updates_describe_the_same_commit():
    snapshot = {"plan_id": "P", "revision": 9, "todos": [
        {"id": "reminder", "content": "reminder", "status": "pending", "meta_data": {"persistent": True}},
        {"id": "active", "content": "active", "status": "in_progress"},
        {"id": "cancelled", "content": "cancelled", "status": "cancelled"},
    ]}
    session = SimpleNamespace(get_session_id=lambda: "session", write_stream=AsyncMock())
    task_rail = TaskExecutionRail()
    stream_rail = object.__new__(JiuSwarmStreamEventRail)
    stream_rail._member_name = "member"
    token = TODO_SNAPSHOT_MODE.set("versioned")
    try:
        await task_rail._emit_task_update_event(session, snapshot=snapshot)
        await stream_rail._emit_todo_updated(session, "session", tool_name="todo_modify", snapshot=snapshot)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    task_payload, todo_payload = [call.args[0].payload for call in session.write_stream.call_args_list]
    assert task_payload["todo_meta"] == todo_payload["todo_meta"] == {"plan_id": "P", "revision": 9}
    assert [(task["task_id"], task["status"]) for task in task_payload["tasks"]] == [
        (todo["id"], todo["status"]) for todo in todo_payload["todos"]
    ]


@pytest.mark.parametrize("mode", ["legacy", "versioned"])
@pytest.mark.parametrize("old_array", [False, True])
def test_external_completion_uses_the_consumer_policy(tmp_path, monkeypatch, mode, old_array):
    todos = [
        {"id": "A", "content": "active", "status": "in_progress"},
        {"id": "B", "content": "completed", "status": "completed"},
    ]
    path = tmp_path / "todo.json"
    path.write_text(json.dumps(todos if old_array else {"plan_id": "P", "revision": 7, "todos": todos}))
    rail = TaskExecutionRail()
    monkeypatch.setattr(rail, "_get_todo_workspace_path", lambda _sid: path)
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        complete, update = rail.external_todo_completion_events("session", "B")
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    if mode == "legacy":
        assert "todo_meta" not in complete.payload and "todo_meta" not in update.payload
        assert update.payload["tasks"][1]["status"] == "pending"
    else:
        meta = {"plan_id": "legacy", "revision": 0} if old_array else {"plan_id": "P", "revision": 7}
        assert complete.payload["todo_meta"] == update.payload["todo_meta"] == meta
        assert update.payload["tasks"][1]["status"] == "completed"
