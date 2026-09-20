# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron execution session identity helpers."""

from __future__ import annotations


def is_cron_execution_session(session_id: str | None) -> bool:
    """Return True for scheduled-job execution sessions (``cron_*``).

    Cron runs have no operator on the other end, so interactive permission
    interrupts cannot be answered and must not be attached.
    """
    return str(session_id or "").strip().startswith("cron")
