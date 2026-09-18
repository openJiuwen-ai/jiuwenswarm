# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native subagent events project beside dest task.* / stream_source_id."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.subagent_stream import (
    clear_all_subagent_progress_batches,
    clear_subagent_progress_batch,
    project_subagent_updated_for_web,
    resolve_subagent_parallel_fields,
    try_handle_subagent_chunk,
)
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def setup_function() -> None:
    clear_all_subagent_progress_batches()


def test_parallel_fields_use_dynamic_total() -> None:
    first = resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-a",
        legacy_status="starting",
    )
    second = resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-b",
        legacy_status="starting",
    )
    assert first == (0, 1, False)
    assert second == (1, 2, True)
    done = resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-a",
        legacy_status="completed",
    )
    assert done == (0, 2, True)
    leftover = resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-b",
        legacy_status="starting",
    )
    assert leftover == (0, 1, False)


def test_project_keeps_canonical_status() -> None:
    payload = project_subagent_updated_for_web(
        {
            "subagent_id": "sa-1",
            "parent_session_id": "parent1",
            "status": "running",
            "display_name": "Explore",
        }
    )
    assert payload["event_type"] == "chat.subtask_update"
    assert payload["session_id"] == "parent1"
    assert payload["status"] == "running"
    assert payload["legacy_status"] == "starting"
    assert payload["task_id"] == "sa-1"
    assert payload["description"] == "Explore"
    assert payload["total"] >= 1


def test_parse_stream_chunk_maps_activity_and_keeps_task_complete() -> None:
    activity = try_handle_subagent_chunk(
        "subagent_activity",
        {
            "subagent_activity": {
                "subagent_id": "sa-1",
                "parent_session_id": "parent1",
                "summary": "searching",
            }
        },
    )
    assert activity[0] is True
    assert activity[1] is not None
    assert activity[1]["event_type"] == "chat.subagent_activity"
    assert activity[1]["session_id"] == "parent1"

    task_chunk = SimpleNamespace(
        type="task.complete",
        payload={
            "task_id": "todo:1",
            "task_content": "done",
            "status": "completed",
        },
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(task_chunk)
    assert parsed is not None
    assert parsed["event_type"] == "task.complete"
    assert parsed["task_id"] == "todo:1"


def test_stream_utils_keeps_stream_source_id_on_activity() -> None:
    chunk = SimpleNamespace(
        type="subagent_activity",
        payload={
            "subagent_activity": {
                "subagent_id": "sa-1",
                "parent_session_id": "parent1",
                "summary": "x",
            },
            "stream_source_id": "skill-turbo-1",
        },
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["event_type"] == "chat.subagent_activity"
    assert parsed["stream_source_id"] == "skill-turbo-1"


def test_message_chunk_persists_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    handled, parsed = try_handle_subagent_chunk(
        "subagent_message",
        {
            "subagent_message": {
                "subagent_id": "sa-1",
                "parent_session_id": "parent1",
                "content": "hello child",
                "role": "assistant",
                "seq": 3,
            }
        },
    )
    assert handled is True
    assert parsed is None
    rows = session_history.load_history_records("parent1", subagent_id="sa-1")
    assert any(row.get("content") == "hello child" for row in rows)
    assert session_history.load_history_records("parent1") == []


def test_release_clears_parent_batch() -> None:
    resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-a",
        legacy_status="starting",
    )
    clear_subagent_progress_batch("parent1")
    again = resolve_subagent_parallel_fields(
        parent_session_id="parent1",
        subagent_id="sa-b",
        legacy_status="starting",
    )
    assert again == (0, 1, False)
