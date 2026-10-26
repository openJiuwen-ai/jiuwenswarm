# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record completed tasks into the request summary accumulator."""

from __future__ import annotations

import logging

from jiuwenswarm.perf.collector import get_perf_collector
from jiuwenswarm.perf.config import get_perf_summary_config
from jiuwenswarm.perf.events import TaskPerfEvent

logger = logging.getLogger(__name__)


def notify_task_complete(
    *,
    task_id: str,
    task_content: str,
    source: str,
    started_at: float,
    ended_at: float,
    duration_ms: float,
    status: str,
    request_id: str,
) -> None:
    """Record a completed main-agent todo into the request summary."""
    cfg = get_perf_summary_config()
    if not cfg.enabled or not cfg.include_tasks:
        return

    rid = (request_id or "").strip()
    if not rid:
        logger.debug(
            "[perf] skip task.complete record: no request_id task_id=%s",
            task_id,
        )
        return

    get_perf_collector().record_task(
        rid,
        TaskPerfEvent(
            task_id=task_id,
            task_content=task_content,
            source=source,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=float(duration_ms),
            status=status,
        ),
    )
