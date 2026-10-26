# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.tools.web_search.constants import (
    MIN_ANSWER_ONLY_LEN,
    MIN_SNIPPET_AVG_LEN,
)
from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchRecord


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_valid_record(record: WebSearchRecord) -> bool:
    title = (record.title or "").strip()
    if not title:
        record.title = "(无标题)"
    return bool(record.title) and _is_valid_http_url(record.url)


def evaluate_search_quality(
    records: list[WebSearchRecord],
    *,
    answer: str = "",
    max_results: int = 8,
    skip_snippet_check: bool = False,
) -> tuple[bool, str]:
    valid = [item for item in records if _is_valid_record(item)]
    min_required = max(1, min(2, (max_results + 1) // 2))
    text = (answer or "").strip()
    has_rich_answer = len(text) >= MIN_ANSWER_ONLY_LEN

    if not valid:
        if has_rich_answer:
            return True, "answer_only"
        return False, "no_valid_records"

    if len(valid) < min_required:
        if has_rich_answer:
            return True, "answer_with_citations"
        return False, f"valid_count={len(valid)}<{min_required}"

    if has_rich_answer and skip_snippet_check:
        return True, "answer_with_citations"

    snippets = [item.snippet.strip() for item in valid if (item.snippet or "").strip()]
    if snippets:
        avg_len = sum(len(item) for item in snippets) / len(snippets)
        if avg_len < MIN_SNIPPET_AVG_LEN:
            if has_rich_answer:
                return True, "answer_with_citations"
            return False, f"avg_snippet_len={avg_len:.0f}<{MIN_SNIPPET_AVG_LEN}"
    elif len(valid) < min_required + 1:
        if has_rich_answer or skip_snippet_check:
            return True, "answer_with_citations" if has_rich_answer else "urls_only"
        return False, "no_snippets"
    return True, "ok"
