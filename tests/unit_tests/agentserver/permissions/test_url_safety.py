# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared AutoReviewer URL safety helpers."""

from __future__ import annotations

# TEST ONLY: URL and credential-shaped fixtures are synthetic. URLs use
# RFC-reserved domains or blocked security-test addresses; policy evaluation
# rejects or parses them without network I/O.

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    UserIntentSource,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import (
    RecentUrlSource,
    evaluate_reviewable_url_scope,
    normalize_url_for_match,
)


def _host_intent(text: str) -> OriginalUserIntentEvidence:
    return OriginalUserIntentEvidence(
        source=UserIntentSource.HOST_USER_MESSAGE,
        text=text,
    )


def _facts(tool_name: str, args: dict[str, object], *, workspace_root: Path):
    return build_tool_decision_facts(
        tool_name,
        args,
        workspace_root=workspace_root,
        original_args_were_valid_object=True,
    )


def test_reviewable_url_scope_accepts_explicit_user_url(tmp_path: Path) -> None:
    url = "https://example.invalid/docs?a=1"
    facts = _facts(
        "mcp_fetch_webpage", {"url": url}, workspace_root=tmp_path
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent(f"Open {url}"),
    )

    assert decision.accepted is True
    assert decision.evidence_summary["source_kind"] == "explicit_user_intent"


def test_reviewable_url_scope_accepts_exact_trusted_search_result(
    tmp_path: Path,
) -> None:
    url = "https://news.example.test/article?id=1"
    facts = _facts(
        "mcp_fetch_webpage", {"url": url}, workspace_root=tmp_path
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent("查找今天的公开新闻"),
        recent_url_sources=(
            RecentUrlSource(
                url=url,
                host="news.example.test",
                source_tool="host_search_producer",
                trusted=True,
            ),
        ),
    )

    assert decision.accepted is True
    assert decision.evidence_summary["source_kind"] == "recent_search_result"


def test_reviewable_url_scope_requires_exact_canonical_path_and_query(
    tmp_path: Path,
) -> None:
    source = "https://news.example.test/article?id=1"
    facts = _facts(
        "mcp_fetch_webpage",
        {"url": "https://news.example.test/article?id=2"},
        workspace_root=tmp_path,
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent("继续"),
        recent_url_sources=(
            RecentUrlSource(
                url=source,
                host="news.example.test",
                source_tool="host_search_producer",
                trusted=True,
            ),
        ),
    )

    assert decision.accepted is True
    assert decision.semantic_review_required is True
    assert decision.evidence_summary["source_kind"] == ""


@pytest.mark.parametrize(
    "intent",
    (
        "Find a current security advisory and summarize it.",
        "Do not browse the internet; explain whether this URL is relevant.",
        "Review local notes and decide whether this public page is needed.",
        "查找近期公开的安全公告并总结。",
        "不要访问互联网；只判断这个链接是否相关。",
        "只检查本地笔记，再判断是否需要这个公开页面。",
    ),
)
def test_safe_unbound_url_defers_intent_to_semantic_reviewer(
    tmp_path: Path,
    intent: str,
) -> None:
    facts = _facts(
        "mcp_fetch_webpage",
        {"url": "https://news.example.test/article"},
        workspace_root=tmp_path,
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent(intent),
    )

    assert decision.accepted is True
    assert decision.semantic_review_required is True
    assert decision.evidence_summary["source_kind"] == ""


def test_safe_url_with_empty_host_intent_stays_manual_only(tmp_path: Path) -> None:
    facts = _facts(
        "mcp_fetch_webpage",
        {"url": "https://news.example.test/article"},
        workspace_root=tmp_path,
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent(""),
    )

    assert decision.accepted is False
    assert decision.reason == "original_user_intent_missing"


@pytest.mark.parametrize(
    ("url", "reason"),
    (
        ("http://example.invalid", "network_scheme_not_https"),
        ("https://169.254.169.254/latest", "network_metadata_host"),
        ("https://example.local/path", "network_internal_hostname"),
        ("https://user:password@example.invalid", "network_url_userinfo"),
        ("https://example.invalid/?api_key=secret", "network_secret_query"),
    ),
)
def test_reviewable_url_scope_hard_blocks_unsafe_urls(
    tmp_path: Path,
    url: str,
    reason: str,
) -> None:
    facts = _facts(
        "mcp_fetch_webpage", {"url": url}, workspace_root=tmp_path
    )

    decision = evaluate_reviewable_url_scope(
        facts,
        evidence=_host_intent(f"Open {url}"),
    )

    assert decision.accepted is False
    assert decision.reason == reason
    assert decision.hard_block is True


def test_normalize_url_for_match_dedupes_host_case_only() -> None:
    assert normalize_url_for_match("HTTPS://Example.INVALID/a?q=1") == (
        "https://example.invalid/a?q=1"
    )
    assert normalize_url_for_match("https://example.invalid/a?q=2") != (
        normalize_url_for_match("https://example.invalid/a?q=1")
    )
