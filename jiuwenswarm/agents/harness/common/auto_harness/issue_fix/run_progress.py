# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue-fix run-log status extensions."""

from __future__ import annotations

import re
from typing import Any

_LEGACY_SKIP_STAGE_MARKERS = {
    "显式 GitCode issue 修复任务，跳过 assess/plan": ("assess", "plan"),
}


def infer_issue_fix_skipped_stages(content: str) -> tuple[str, ...]:
    """Normalize issue-fix legacy skip messages into structured skipped stages."""
    return next(
        (
            stages for marker, stages in _LEGACY_SKIP_STAGE_MARKERS.items()
            if marker in content
        ),
        (),
    )


def _extract_gitcode_pr_url(text: str) -> str:
    for url in re.findall(r"https://gitcode\.com/[^\s)>\"]+", text):
        if re.search(r"/(?:pulls|pull_requests|merge_requests)/\d+", url):
            return url
    return ""


def _classify_issue_fix_failure(error: str, last_message: str = "") -> str:
    text = f"{error}\n{last_message}".lower()
    if "missing required labels" in text:
        return "missing_required_labels"
    if (
        "gitcode pr creation failed" in text
        or "http error 400" in text
        or "bad request" in text
    ):
        return "pr_api_failed"
    return ""


def enrich_issue_fix_progress(
    progress: dict[str, Any],
    logs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add GitCode issue-fix specific diagnostics to generic progress."""
    enriched = dict(progress)
    pr_url = str(enriched.get("pr_url") or "")
    for entry in logs:
        text_parts = [
            str(entry.get("content") or ""),
            str(entry.get("message") or ""),
        ]
        text_parts.extend(str(message) for message in entry.get("messages") or [])
        for text in text_parts:
            if not pr_url:
                pr_url = _extract_gitcode_pr_url(text)
    if pr_url:
        enriched["pr_url"] = pr_url

    if enriched.get("failed_stage"):
        issue_failure_code = _classify_issue_fix_failure(
            str(enriched.get("last_error") or ""),
            str(enriched.get("last_message") or ""),
        )
        if issue_failure_code:
            enriched["failure_code"] = issue_failure_code
    return enriched
