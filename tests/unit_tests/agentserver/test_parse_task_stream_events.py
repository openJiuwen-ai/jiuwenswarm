# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Forward TaskExecutionRail task.* chunks to frontend-consumable events."""

from types import SimpleNamespace

from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def test_event_type_accepts_task_lifecycle_values() -> None:
    assert EventType("task.start") is EventType.TASK_START
    assert EventType("task.update") is EventType.TASK_UPDATE
    assert EventType("task.complete") is EventType.TASK_COMPLETE


def test_parse_stream_chunk_forwards_task_update_snapshot() -> None:
    payload = {
        "tasks": [
            {"id": "todo:1", "content": "写封面", "status": "pending"},
            {"id": "todo:2", "content": "写目录", "status": "pending"},
        ],
        "total_tasks": 2,
        "completed_tasks": 0,
        "in_progress_tasks": 0,
        "pending_tasks": 2,
        "parent_request_id": "req-1",
        "timestamp": 1.5,
    }
    chunk = SimpleNamespace(type="task.update", payload=payload)

    expected = {
        "event_type": "task.update",
        "tasks": payload["tasks"],
        "total_tasks": 2,
        "completed_tasks": 0,
        "in_progress_tasks": 0,
        "pending_tasks": 2,
        "parent_request_id": "req-1",
        "timestamp": 1.5,
    }
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) == expected
    assert parse_stream_chunk(chunk) == expected


def test_parse_stream_chunk_forwards_task_start_and_complete() -> None:
    start_chunk = SimpleNamespace(
        type="task.start",
        payload={
            "task_id": "todo:1",
            "task_content": "写封面",
            "task_index": 0,
            "total_tasks": 2,
            "parent_request_id": "req-1",
            "timestamp": 1.0,
        },
    )
    complete_chunk = SimpleNamespace(
        type="task.complete",
        payload={
            "task_id": "todo:1",
            "task_content": "写封面",
            "status": "completed",
            "duration_ms": 12,
            "error": None,
            "timestamp": 2.0,
        },
    )

    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(start_chunk) == {
        "event_type": "task.start",
        "task_id": "todo:1",
        "task_content": "写封面",
        "task_index": 0,
        "total_tasks": 2,
        "parent_request_id": "req-1",
        "timestamp": 1.0,
    }
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(complete_chunk) == {
        "event_type": "task.complete",
        "task_id": "todo:1",
        "task_content": "写封面",
        "status": "completed",
        "duration_ms": 12,
        "error": None,
        "timestamp": 2.0,
    }


def test_parse_stream_chunk_drops_task_update_without_payload_dict() -> None:
    chunk = SimpleNamespace(type="task.update", payload="not-a-dict")
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None
    assert parse_stream_chunk(chunk) is None
