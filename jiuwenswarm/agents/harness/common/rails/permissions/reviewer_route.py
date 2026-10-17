"""Compact Host routing for the stateless semantic reviewer."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    ALLOW_LEVEL,
    ASK_LEVEL,
    DENY_LEVEL,
)
from jiuwenswarm.agents.harness.common.rails.permissions.execution_provider_contract import (
    EXECUTION_PROVIDER_CONTRACT_UNVERIFIED,
    requires_manual_execution_provider_review,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import (
    RecentUrlSource,
    evaluate_reviewable_url_scope,
)

SEMANTIC_REVIEW_SOURCE = "semantic_reviewer"
MANUAL_REVIEW_SOURCE = "manual_only"
HARD_BLOCK_SOURCE = "hard_guard"
RECENT_FETCH_SOURCE = "recent_search_result"

ALLOWABLE_REVIEWER_OUTCOMES = ("allow_once", "manual", "deny")
WORKSPACE_WRITE_TOOLS = frozenset(
    {"apply_patch", "write_file", "edit_file", "write_text_file", "write", "search_replace"}
)
USER_PATH_TOOLS = frozenset(
    {
        "glob",
        "glob_file_search",
        "grep",
        "list_dir",
        "list_files",
        "read",
        "read_file",
        "read_text_file",
        "write_file",
        "write_text_file",
    }
)
DEFAULT_DELIVERY_EXCLUDED_PATHS = (
    ".git",
    ".github",
    ".gitlab-ci.yml",
    ".circleci",
    ".buildkite",
    ".envrc",
    ".env",
    ".env.local",
    ".ssh",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".config/fish/config.fish",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "AGENTS.md",
    "CLAUDE.md",
    "Dockerfile",
    "Jenkinsfile",
    "Cargo.lock",
    "Cargo.toml",
    "Gemfile",
    "Gemfile.lock",
    "Pipfile",
    "Pipfile.lock",
    "bun.lock",
    "bun.lockb",
    "composer.json",
    "composer.lock",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pyproject.toml",
    "requirements",
    "requirements-dev.txt",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "yarn.lock",
    "approval_overrides",
    "config/config.yaml",
    "config/permissions.tools",
    "config/permissions.yaml",
    "deploy",
    "deployment",
    "docker-compose.yaml",
    "docker-compose.yml",
    "helm",
    "k8s",
    "permissions.tools",
    "permissions.yaml",
)


def reviewer_route(
    facts: ToolDecisionFacts,
    *,
    policy_level: str,
    guard_result: str,
    workspace_root: Path | str | None,
    delivery_max_files: int = 3,
    delivery_excluded_paths: tuple[str, ...] = (),
    original_user_intent: OriginalUserIntentEvidence | None = None,
    domain_route: DecisionRoute | None = None,
    recent_url_sources: tuple[RecentUrlSource, ...] = (),
) -> DecisionRoute:
    """Return the single Host route for one already-policy-evaluated call."""

    policy = str(policy_level or "").strip().lower()
    guard = str(guard_result or "").strip().lower()
    if policy == DENY_LEVEL:
        return _deny("policy_deny")
    if guard == DENY_LEVEL:
        return _deny("guard_deny")
    if policy not in {ASK_LEVEL, ALLOW_LEVEL} and guard != "scoped_candidate":
        return _deny("policy_not_ask")
    if guard == "terminal_manual":
        return _manual("terminal_manual")

    structural_reason = _structural_manual_reason(facts)
    if structural_reason:
        return _manual(structural_reason)

    if domain_route is not None:
        if domain_route.is_hard_block:
            return _deny(domain_route.reason or "domain_policy_deny")
        if domain_route.requires_manual:
            return _manual(domain_route.reason or "domain_policy_manual")

    if requires_manual_execution_provider_review(
        facts.tool_name,
        tool_category=facts.capability.category,
    ):
        return _manual(EXECUTION_PROVIDER_CONTRACT_UNVERIFIED)

    if facts.tool_name == "send_file_to_user":
        delivery_reason = file_delivery_manual_reason(
            facts,
            workspace_root=workspace_root,
            max_files=delivery_max_files,
            excluded_paths=delivery_excluded_paths,
        )
        if delivery_reason:
            return _manual(delivery_reason)

    if _is_workspace_write(facts):
        if workspace_root is None:
            return _manual("bounded_write_workspace_missing")
        return _semantic()
    if facts.tool_name in USER_PATH_TOOLS and facts.external_paths:
        return _semantic()

    if _is_public_search(facts):
        query = str(facts.untrusted_args.get("query") or "").strip()
        if not query:
            return _manual("search_query_missing")
        if _search_query_is_secret_like(query):
            return _deny("search_query_secret_like")

    if _is_public_fetch(facts):
        url_decision = evaluate_reviewable_url_scope(
            facts,
            evidence=original_user_intent,
            recent_url_sources=recent_url_sources,
        )
        if url_decision.hard_block:
            return _deny(url_decision.reason)
        if not url_decision.accepted:
            return _manual(url_decision.reason or "network_url_scope_rejected")
        source_kind = str(url_decision.evidence_summary.get("source_kind") or "")
        if source_kind == RECENT_FETCH_SOURCE:
            return DecisionRoute(
                ALLOW_LEVEL,
                "deterministic_readonly_public_web",
                RECENT_FETCH_SOURCE,
            )

    return _semantic()


def _structural_manual_reason(facts: ToolDecisionFacts) -> str:
    if not facts.arguments_valid_object:
        return "arguments_not_object"
    if facts.capability.category in {"path", "shell"} and not facts.accesses_known:
        return "core_accesses_unknown"
    if facts.capability.alias_conflict:
        return "alias_conflict"
    if facts.capability.facts_source != "host_static":
        return "facts_source_unverified"
    if (
        facts.capability.operation_family == "unknown"
        or facts.capability.category == "unknown"
    ):
        return "unknown_operation"
    return ""


def _is_public_search(facts: ToolDecisionFacts) -> bool:
    return (
        facts.capability.operation_family == "public_search"
        and facts.capability.category == "network"
    )


def _is_workspace_write(facts: ToolDecisionFacts) -> bool:
    return (
        facts.tool_name in WORKSPACE_WRITE_TOOLS
        and facts.capability.category == "path"
        and facts.capability.risk_tier == "medium"
        and bool(facts.write_paths)
    )


def file_delivery_manual_reason(
    facts: ToolDecisionFacts,
    *,
    workspace_root: Path | str | None,
    max_files: int,
    excluded_paths: tuple[str, ...],
) -> str:
    if workspace_root is None:
        return "bounded_user_file_delivery_workspace_missing"
    read_paths = facts.read_paths
    if not read_paths:
        return "bounded_user_file_delivery_missing_path"
    if facts.external_paths:
        return "bounded_user_file_delivery_external_path"
    if len(read_paths) > max_files:
        return "bounded_user_file_delivery_too_many_files"
    root = Path(workspace_root).expanduser().resolve(strict=False)
    exclusions = DEFAULT_DELIVERY_EXCLUDED_PATHS + excluded_paths
    for raw_path in read_paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        for excluded in exclusions:
            if _path_matches_exclusion(path, excluded_path=excluded, workspace_root=root):
                return "bounded_user_file_delivery_excluded_path"
    return ""


def _path_matches_exclusion(
    candidate: Path,
    *,
    excluded_path: str,
    workspace_root: Path,
) -> bool:
    text = str(excluded_path or "").strip()
    if not text:
        return False
    excluded = Path(text).expanduser()
    if excluded.is_absolute():
        return candidate.is_relative_to(excluded.resolve(strict=False))
    if candidate.is_relative_to((workspace_root / excluded).resolve(strict=False)):
        return True
    candidate_parts = tuple(part.casefold() for part in candidate.parts)
    excluded_parts = tuple(
        part.casefold() for part in text.replace("\\", "/").strip("/").split("/") if part
    )
    if len(excluded_parts) == 1:
        return bool(excluded_parts and excluded_parts[0] in candidate_parts)
    width = len(excluded_parts)
    return bool(
        width
        and any(
            candidate_parts[index:index + width] == excluded_parts
            for index in range(len(candidate_parts) - width + 1)
        )
    )


def _is_public_fetch(facts: ToolDecisionFacts) -> bool:
    return (
        facts.capability.operation_family == "public_web_read"
        and facts.capability.category == "network"
    )


def _search_query_is_secret_like(query: str) -> bool:
    text = unicodedata.normalize("NFKC", str(query or ""))
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    key = r"(?:api[_\s-]*key|apikey|authorization|bearer|password|passwd|secret|token)"
    token = (
        r"(?:sk-(?:proj-)?[a-z0-9_-]{12,}|ghp_[a-z0-9_]{20,}|"
        r"github_pat_[a-z0-9_]{20,}|akia[a-z0-9]{16}|"
        r"[a-z0-9][a-z0-9_.-]{19,})"
    )
    return bool(
        re.search(rf"['\"]?\b{key}\b['\"]?\s*[:=]", normalized)
        or re.search(rf"\b{key}\b\s+(?:is\s+)?['\"]?{token}", normalized)
        or re.search(r"\bsk-(?:proj-)?[a-z0-9_-]{12,}\b", normalized)
        or re.search(r"\b(?:ghp|github_pat)_[a-z0-9_]{20,}\b", normalized)
        or re.search(r"\bakia[a-z0-9]{16}\b", normalized)
    )


def _manual(reason: str) -> DecisionRoute:
    return DecisionRoute(ASK_LEVEL, reason, MANUAL_REVIEW_SOURCE)


def _deny(reason: str) -> DecisionRoute:
    return DecisionRoute(DENY_LEVEL, reason, HARD_BLOCK_SOURCE)


def _semantic() -> DecisionRoute:
    return DecisionRoute(ASK_LEVEL, "semantic_review_required", SEMANTIC_REVIEW_SOURCE)
