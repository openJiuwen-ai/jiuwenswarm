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


# Origin: 自拟 §6.H. Dest projection contract. Dump-file golden lives in test_debug_trace.
_SUBAGENT_UPDATED_WEB_GOLDEN = {
    "event_type": "chat.subtask_update",
    "subagent_id": "sa-1",
    "parent_session_id": "parent1",
    "status": "running",
    "display_name": "Explore",
    "session_id": "parent1",
    "task_id": "sa-1",
    "description": "Explore",
    "legacy_status": "starting",
    "index": 0,
    "total": 1,
    "is_parallel": False,
}


def test_subagent_updated_payload_golden() -> None:
    """Origin: 自拟 §6.H. Projection payload contract beside the WS-shaped sequence."""
    payload = project_subagent_updated_for_web(
        {
            "subagent_id": "sa-1",
            "parent_session_id": "parent1",
            "status": "running",
            "display_name": "Explore",
        }
    )
    assert payload == _SUBAGENT_UPDATED_WEB_GOLDEN


def test_subagent_updated_chunk_keeps_dest_task_fields(tmp_path, monkeypatch) -> None:
    """Origin: 自拟 §6.H. One projected chunk still carries dest task fields."""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    handled, parsed = try_handle_subagent_chunk(
        "subagent_updated",
        {
            "subagent_updated": {
                "subagent_id": "sa-1",
                "parent_session_id": "parent1",
                "status": "running",
                "display_name": "Explore",
            }
        },
    )
    assert handled is True
    assert parsed is not None
    assert parsed["event_type"] == "chat.subtask_update"
    assert parsed["session_id"] == "parent1"
    assert parsed["task_id"] == "sa-1"
    assert parsed["legacy_status"] == "starting"
    assert parsed["status"] == "running"
    assert "task.start" != parsed["event_type"]


def test_subagent_chunk_sequence_replays_three_children_and_isolates_sessions(
    tmp_path,
    monkeypatch,
) -> None:
    """Origin: 自拟 §6.H. WS-shaped chunk sequence + history + session isolation.

    Not a live WebSocket. Feeds the dest projector the same event kinds the
    frontend store replay uses: running, activity, idle/completed, failed.
    """
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    parent = "parent-h"
    other = "parent-other"
    children = ("sa-a", "sa-b", "sa-c")

    emitted: list[dict] = []
    for index, child_id in enumerate(children):
        handled, running = try_handle_subagent_chunk(
            "subagent_updated",
            {
                "subagent_updated": {
                    "subagent_id": child_id,
                    "parent_session_id": parent,
                    "status": "running",
                    "display_name": f"Child {child_id}",
                    "revision": 1,
                    "updated_at_ms": 1000 + index,
                }
            },
        )
        assert handled is True
        assert running is not None
        assert running["task_id"] == child_id
        emitted.append(running)
        handled, activity = try_handle_subagent_chunk(
            "subagent_activity",
            {
                "subagent_activity": {
                    "subagent_id": child_id,
                    "parent_session_id": parent,
                    "task_id": f"task-{child_id}",
                    "seq": 1,
                    "kind": "thinking",
                    "summary": f"thinking {child_id}",
                    "at_ms": 1100 + index,
                }
            },
        )
        assert handled is True
        assert activity is not None
        assert activity["event_type"] == "chat.subagent_activity"
        emitted.append(activity)

    handled, idle = try_handle_subagent_chunk(
        "subagent_updated",
        {
            "subagent_updated": {
                "subagent_id": "sa-a",
                "parent_session_id": parent,
                "status": "idle",
                "display_name": "Child sa-a",
                "revision": 2,
                "updated_at_ms": 2000,
            }
        },
    )
    assert handled is True
    assert idle is not None
    assert idle["legacy_status"] == "completed"
    handled, failed = try_handle_subagent_chunk(
        "subagent_updated",
        {
            "subagent_updated": {
                "subagent_id": "sa-c",
                "parent_session_id": parent,
                "status": "closed",
                "display_name": "Child sa-c",
                "revision": 2,
                "updated_at_ms": 2100,
                "closed_reason": "failed",
            }
        },
    )
    assert handled is True
    assert failed is not None
    assert failed["legacy_status"] == "error"

    handled, other_running = try_handle_subagent_chunk(
        "subagent_updated",
        {
            "subagent_updated": {
                "subagent_id": "sa-a",
                "parent_session_id": other,
                "status": "running",
                "display_name": "Other child",
                "revision": 1,
                "updated_at_ms": 3000,
            }
        },
    )
    assert handled is True
    assert other_running is not None

    for child_id in children:
        rows = session_history.load_history_records(parent, subagent_id=child_id)
        assert rows
        assert any(row.get("event_type") == "chat.subtask_update" for row in rows)
    assert session_history.load_history_records(parent) == []
    parent_a = session_history.load_history_records(parent, subagent_id="sa-a")
    other_rows = session_history.load_history_records(other, subagent_id="sa-a")
    assert parent_a
    assert other_rows
    assert parent_a != other_rows
    leftover = resolve_subagent_parallel_fields(
        parent_session_id=parent,
        subagent_id="sa-b",
        legacy_status="starting",
    )
    assert leftover == (0, 1, False)
    other_batch = resolve_subagent_parallel_fields(
        parent_session_id=other,
        subagent_id="sa-a",
        legacy_status="starting",
    )
    assert other_batch == (0, 1, False)
    assert any(item.get("subagent_id") == "sa-a" for item in emitted)
    assert any(item.get("subagent_id") == "sa-c" for item in emitted)
