"""Current contracts for the compact Reviewer Host route."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved domains and are evaluated as plain
# policy data without external network I/O.

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_route import (
    HARD_BLOCK_SOURCE,
    MANUAL_REVIEW_SOURCE,
    RECENT_FETCH_SOURCE,
    SEMANTIC_REVIEW_SOURCE,
    file_delivery_manual_reason,
    reviewer_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    UserIntentSource,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import RecentUrlSource


def _facts(
    tool_name: str,
    args: dict[str, object],
    root: Path,
    *,
    external_paths: tuple[str, ...] = (),
    send_paths: tuple[str, ...] = (),
):
    return build_tool_decision_facts(
        tool_name,
        args,
        workspace_root=root,
        original_args_were_valid_object=True,
        external_paths=external_paths,
        send_paths=send_paths,
    )


@pytest.mark.parametrize(
    ("paths", "max_files", "excluded_paths", "external", "expected_reason"),
    [
        ((), 3, (), (), "bounded_user_file_delivery_missing_path"),
        (("outside.txt",), 3, (), ("outside.txt",), "bounded_user_file_delivery_external_path"),
        (("one.txt", "two.txt"), 1, (), (), "bounded_user_file_delivery_too_many_files"),
        ((".git/config",), 3, (), (), "bounded_user_file_delivery_excluded_path"),
        (("report.txt",), 3, (), (), ""),
    ],
)
def test_file_delivery_uses_file_guard_paths(
    tmp_path: Path,
    paths: tuple[str, ...],
    max_files: int,
    excluded_paths: tuple[str, ...],
    external: tuple[str, ...],
    expected_reason: str,
) -> None:
    resolved = tuple((tmp_path / path).as_posix() for path in paths)
    external_resolved = tuple((tmp_path / path).as_posix() for path in external)
    facts = _facts(
        "send_file_to_user",
        {"abs_file_path_list": list(resolved)},
        tmp_path,
        external_paths=external_resolved,
        send_paths=resolved,
    )

    assert file_delivery_manual_reason(
        facts,
        workspace_root=tmp_path,
        max_files=max_files,
        excluded_paths=excluded_paths,
    ) == expected_reason


@pytest.mark.parametrize(
    ("policy_level", "guard_result", "expected_source"),
    [
        ("ask", "not_applicable", SEMANTIC_REVIEW_SOURCE),
        ("deny", "not_applicable", HARD_BLOCK_SOURCE),
        ("ask", "deny", HARD_BLOCK_SOURCE),
        ("ask", "terminal_manual", MANUAL_REVIEW_SOURCE),
    ],
)
def test_policy_and_guard_precedence(
    tmp_path: Path,
    policy_level: str,
    guard_result: str,
    expected_source: str,
) -> None:
    facts = _facts("read_file", {"path": "README.md"}, tmp_path)
    route = reviewer_route(
        facts,
        policy_level=policy_level,
        guard_result=guard_result,
        workspace_root=tmp_path,
    )
    assert route.source == expected_source


@pytest.mark.parametrize(
    ("domain_route", "expected_source"),
    [
        (DecisionRoute("deny", "blocked", "hard_guard"), HARD_BLOCK_SOURCE),
        (DecisionRoute("ask", "manual", "manual_only"), MANUAL_REVIEW_SOURCE),
        (DecisionRoute("ask", "review", "semantic_reviewer"), SEMANTIC_REVIEW_SOURCE),
    ],
)
def test_domain_route_sets_reviewer_ceiling(
    tmp_path: Path,
    domain_route: DecisionRoute,
    expected_source: str,
) -> None:
    facts = _facts("install_skill", {"skill_id": "markdown-lint"}, tmp_path)
    route = reviewer_route(
        facts,
        policy_level="ask",
        guard_result="scoped_candidate",
        workspace_root=tmp_path,
        domain_route=domain_route,
    )
    assert route.source == expected_source


def test_exact_and_recent_fetch_routes(tmp_path: Path) -> None:
    url = "https://docs.example.invalid/3/"
    facts = _facts("mcp_fetch_webpage", {"url": url}, tmp_path)
    explicit = OriginalUserIntentEvidence(
        source=UserIntentSource.HOST_USER_MESSAGE,
        text=f"请读取 {url}",
    )
    discovery = OriginalUserIntentEvidence(
        source=UserIntentSource.HOST_USER_MESSAGE,
        text="查找并阅读近期 Python 文档。",
    )
    recent = (
        RecentUrlSource(
            url=url,
            host="docs.example.invalid",
            source_tool="mcp_free_search",
            trusted=True,
        ),
    )

    explicit_route = reviewer_route(
        facts,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=explicit,
    )
    recent_route = reviewer_route(
        facts,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=discovery,
        recent_url_sources=recent,
    )

    assert explicit_route.source == SEMANTIC_REVIEW_SOURCE
    assert recent_route.source == RECENT_FETCH_SOURCE
