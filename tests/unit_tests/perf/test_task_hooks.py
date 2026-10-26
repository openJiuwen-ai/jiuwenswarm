# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.perf.collector import get_perf_collector
from jiuwenswarm.perf.config import init_perf_summary_config
from jiuwenswarm.perf.context import set_request_context
from jiuwenswarm.perf.task_hooks import notify_task_complete


def test_notify_task_complete_records_task() -> None:
    init_perf_summary_config()
    set_request_context(
        session_id="sess-1",
        request_id="req-task",
        channel_id="web",
        mode="agent.plan",
    )

    notify_task_complete(
        task_id="todo:1",
        task_content="Do work",
        source="todo",
        started_at=1000.0,
        ended_at=1005.0,
        duration_ms=5000.0,
        status="succeeded",
        request_id="req-task",
    )

    acc = get_perf_collector().get_accumulator("req-task")
    assert acc is not None
    assert acc.task_count == 1
    assert acc.tasks[0]["task_content"] == "Do work"
