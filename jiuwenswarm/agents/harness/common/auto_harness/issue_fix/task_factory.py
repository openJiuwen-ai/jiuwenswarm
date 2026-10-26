# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task construction helpers for GitCode issue-fix runs."""

from __future__ import annotations

from openjiuwen.auto_harness.schema import OptimizationTask


def build_issue_fix_task(issue_number: int, query: str) -> OptimizationTask:
    """Build a structured auto-harness task for one GitCode issue."""
    return OptimizationTask(
        topic=f"fix-issue-{issue_number}",
        description=query,
        issue_ref=f"#{issue_number}",
        status="pending",
    )
