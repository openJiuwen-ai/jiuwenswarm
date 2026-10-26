# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""lh-* job identity. Gateway-owned; no task JSON."""

from __future__ import annotations

from typing import Any

LONG_HORIZON_DESC_PREFIX = "[long_horizon:"
STAGE_DUE_EVENT = "long_horizon.stage_due"
EXEC_SESSION_PREFIX = "longhorizon_"


def stage_job_id(task_id: str, stage_id: str) -> str:
    return f"lh-{task_id}-{stage_id}"


def parse_long_horizon_description(description: str) -> tuple[str, str] | None:
    """Return (task_id, stage_id) if description is a long-horizon stage job."""
    text = str(description or "")
    if LONG_HORIZON_DESC_PREFIX not in text:
        return None
    try:
        c_part = text.split(LONG_HORIZON_DESC_PREFIX, 1)[1]
        task_id = c_part.split("]", 1)[0].strip()
        if "[stage:" not in c_part:
            return None
        s_part = c_part.split("[stage:", 1)[1]
        stage_id = s_part.split("]", 1)[0].strip()
        if task_id and stage_id:
            return task_id, stage_id
    except Exception:
        return None
    return None


_MARK_DUE_TERMINAL_ERRORS = frozenset(
    {"task_not_found", "stage_not_found", "task_not_active"}
)


def is_long_horizon_exec_session_id(session_id: str | None) -> bool:
    return str(session_id or "").startswith(EXEC_SESSION_PREFIX)


def should_drop_oneshot_after_mark_due(result: dict[str, Any] | None) -> bool:
    """True when the one-shot lh-* job is safe to delete after wake."""
    payload = result if isinstance(result, dict) else {}
    if payload.get("success"):
        return True
    err = str(payload.get("error") or "").strip()
    if err in _MARK_DUE_TERMINAL_ERRORS:
        return True
    return str(payload.get("error") or "").startswith("stage_already")


def is_long_horizon_cron_job(job: Any) -> bool:
    desc = str(getattr(job, "description", "") or "")
    job_id = str(getattr(job, "id", "") or "")
    if job_id.startswith("lh-"):
        return True
    if isinstance(job, dict) and str(job.get("id") or "").startswith("lh-"):
        return True
    if isinstance(job, dict):
        desc = str(job.get("description") or desc)
    return parse_long_horizon_description(desc) is not None
