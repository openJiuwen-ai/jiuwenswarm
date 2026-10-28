# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Committed task snapshots retain reminder and consumer-output contracts."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    expected_ids = (["reminder"] if include_persistent else []) + ["first", "second"]
    if mode == "versioned":
        expected_ids.append("stale")
        assert payload["todo_meta"] == {"plan_id": "plan", "revision": 7}
    else:
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
