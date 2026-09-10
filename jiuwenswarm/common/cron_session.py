# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron execution session identity helpers."""
from __future__ import annotations


def is_cron_execution_session(session_id: str | None) -> bool:
    """Return True when ``session_id`` represents an unattended cron trigger.

    Cron-triggered sessions use a ``cron``-prefixed session id (e.g.
    ``cron_19abc_job1``) so downstream code (permission interrupt, approval
    prompts, scheduled-task bookkeeping) can detect them and skip any UI
    gating that would otherwise block the unattended run.
    """
    return str(session_id or "").strip().startswith("cron")
