# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the task-level auto reviewer boundary."""

from __future__ import annotations

# TEST ONLY: URL and credential fixtures are synthetic reserved-domain data and
# are projected only into reviewer doubles; no external request is made.

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import (
    reviewer_metadata as reviewer_metadata_module,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AUTO_REVIEW_REASON_SUMMARY_LIMIT,
    AutoReviewer,
    IsolatedModelReviewerClient,
    ReviewerActionView,
    ReviewerOutcome,
    build_reviewer_action_view,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    RootDecisionContext,
    RootIntentTurn,
    RootIntentTurnKind,
    UserIntentSource,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_route import (
    reviewer_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
    build_tool_decision_facts,
)


class StaticReviewerClient:
    """Reviewer client double returning a fixed JSON payload."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[object] = []

    async def assess(self, request: object) -> str:
        """Record and return a static response."""
        self.requests.append(request)
        return self.response


class FailingReviewerClient:
    """Reviewer client double that raises a deterministic failure."""

    async def assess(self, _request: object) -> str:
        """Raise like a failed reviewer backend."""
        raise RuntimeError("reviewer failed")


class UnexpectedReviewerError(Exception):
    """Failure type outside the reviewer's historical exception allowlist."""


class UnexpectedFailingReviewerClient:
    """Reviewer client double raising an arbitrary ordinary exception."""

    async def assess(self, _request: object) -> str:
        """Raise like an unexpected SDK or adapter failure."""
        raise UnexpectedReviewerError("unexpected reviewer failure")


def _build_request(
    *,
    candidate: DecisionRoute,
    policy_reason: str,
    review_evidence: Mapping[str, Any] | None = None,
) -> ReviewerActionView:
    return ReviewerActionView(
        descriptor_summary={},
        policy_reason=policy_reason,
        review_evidence=dict(review_evidence or {}),
        allowed_outcomes=tuple(candidate.allowed_outcomes),
        no_auto_allow_reason=candidate.no_auto_allow_reason,
    )


class TrackingTimeoutReviewerClient:
    """Reviewer client double that records a timed-out invocation."""

    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, request: object) -> str:
        """Sleep past the timeout and record the single attempted call."""
        self.calls += 1
        await asyncio.sleep(0.05)
        return _valid_response(
            request,
            outcome=ReviewerOutcome.ALLOW_ONCE,
            rationale="Readonly fetch is narrow and aligned with the task.",
            extra={"reason_code": "readonly_network_fetch"},
        )


class RecordingIsolatedReviewerModel:
    """Model double recording each fresh reviewer message list."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> object:
        """Record the isolated invocation and return model-like content."""
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content="{}")


class StaticIsolatedReviewerModel:
    """Model double returning one provider-shaped response."""

    def __init__(self, response: object) -> None:
        self.response = response

    async def ainvoke(
        self,
        _messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> object:
        """Return the configured provider response."""
        return self.response


class ConcurrentTrackingReviewerClient:
    """Reviewer client double that records overlapping model calls."""

    def __init__(self) -> None:
        self.active_calls = 0
        self.max_active_calls = 0

    async def assess(self, request: object) -> str:
        """Return a valid response while tracking client concurrency."""
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        await asyncio.sleep(0.01)
        self.active_calls -= 1
        return _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)


def build_facts(
    tool_name: str,
    tool_args: Any,
    **kwargs: Any,
) -> ToolDecisionFacts:
    """Build the same thin facts that the Host runtime supplies."""
    kwargs.setdefault(
        "original_args_were_valid_object",
        isinstance(tool_args, Mapping),
    )
    return build_tool_decision_facts(
        tool_name,
        tool_args if isinstance(tool_args, Mapping) else {},
        **kwargs,
    )


def _candidate(tmp_path: Path) -> tuple[object, object]:
    descriptor = build_facts(
        "read_file",
        {"path": str(tmp_path / "README.md")},
        workspace_root=tmp_path,
    )
    candidate = reviewer_route(
        descriptor,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=None,
        domain_route=None,
    )
    assert candidate.accepted
    return descriptor, candidate


def _manual_only_candidate(tmp_path: Path) -> tuple[object, object]:
    descriptor = build_facts(
        "mcp_fetch_webpage",
        {"url": "https://example.invalid/task-result"},
        workspace_root=tmp_path,
    )
    candidate = reviewer_route(
        descriptor,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=None,
        domain_route=None,
    )
    assert candidate.accepted
    assert candidate.allowed_outcomes == ("manual", "deny")
    return descriptor, candidate


def _readonly_fetch_request(tmp_path: Path) -> object:
    url = "https://search.example.invalid/api/v1/search?query=agent+safety"
    intent_text = f"Fetch {url} and summarize the results."
    descriptor = build_facts(
        "mcp_fetch_webpage",
        {"url": url},
        workspace_root=tmp_path,
    )
    candidate = reviewer_route(
        descriptor,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=OriginalUserIntentEvidence(
            source=UserIntentSource.HOST_USER_MESSAGE,
            text=intent_text,
            context=RootDecisionContext(
                "session-current",
                "request-current",
                "web",
                (
                    RootIntentTurn(
                        request_id="request-current",
                        kind=RootIntentTurnKind.FRESH,
                        text=intent_text,
                    ),
                ),
            ),
        ),
    )
    assert candidate.accepted
    assert candidate.allowed_outcomes == ("allow_once", "manual", "deny")
    return _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )


def _skill_read_request(tmp_path: Path) -> object:
    descriptor = build_facts(
        "skill_tool",
        {"skill_name": "hot-news-pptx"},
        workspace_root=tmp_path,
        original_args_were_valid_object=True,
    )
    candidate = reviewer_route(
        descriptor,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        domain_route=DecisionRoute(
            level="ask",
            reason="domain_policy_skill_readonly",
            source="semantic_reviewer",
        ),
    )
    assert candidate.accepted
    return _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )


def _valid_response(
    _request: object,
    *,
    outcome: str,
    confidence: float = 0.95,
    rationale: str = "The path is a workspace-local read.",
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "outcome": outcome,
        "confidence": confidence,
        "reason_code": "workspace_read",
        "rationale": rationale,
    }
    if extra:
        payload.update(extra)
    if outcome == ReviewerOutcome.MANUAL:
        payload.setdefault("manual_reason_code", "workspace_read")
        payload.setdefault(
            "manual_reason_summary",
            "The path is a workspace-local read.",
        )
        payload.setdefault(
            "user_review_hint",
            "Review the workspace-local read before approving it.",
        )
    return json.dumps(payload)


async def test_auto_reviewer_accepts_valid_allow_once(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.ALLOW_ONCE
    assert assessment.status == "approved"
    assert assessment.reason_code == "workspace_read"
    assert assessment.manual_reason_code == ""
    assert assessment.manual_reason_summary == ""
    assert assessment.user_review_hint == ""
    assert client.requests == [request]


async def test_auto_reviewer_discards_manual_fields_from_allow_once(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(
            request,
            outcome=ReviewerOutcome.ALLOW_ONCE,
            extra={
                "manual_reason_code": "stale_manual",
                "manual_reason_summary": "Review this manually.",
                "user_review_hint": "Approve it manually.",
            },
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.ALLOW_ONCE
    assert assessment.manual_reason_code == ""
    assert assessment.manual_reason_summary == ""
    assert assessment.user_review_hint == ""


@pytest.mark.parametrize(
    ("tool_name", "tool_args", "expected_operation", "expected_target"),
    [
        ("read_file", {"path": "reports/quarterly.pdf"}, "read", "quarterly.pdf"),
        ("write_file", {"path": "outputs/result.xlsx"}, "write", "result.xlsx"),
        ("send_file_to_user", {"path": "ignored"}, "read", "result.xlsx"),
    ],
)
def test_reviewer_path_targets_use_only_core_extracted_accesses(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, str],
    expected_operation: str,
    expected_target: str,
) -> None:
    send_paths = (
        (str(tmp_path / "outputs" / "result.xlsx"),)
        if tool_name == "send_file_to_user"
        else ()
    )
    facts = build_facts(
        tool_name,
        tool_args,
        workspace_root=tmp_path,
        send_paths=send_paths,
    )

    request = build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == [
        {
            "operation": expected_operation,
            "scope": "workspace",
            "target": expected_target,
            "location_status": "complete",
            "location": {
                "base": "workspace",
                "relative_path": (
                    "reports/quarterly.pdf" if tool_name == "read_file"
                    else "outputs/result.xlsx"
                ),
            },
        }
    ]
    serialized = json.dumps(request, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "ignored" not in serialized


@pytest.mark.parametrize(
    "relative_path",
    [".env", ".ssh/id_rsa", "secrets/api_token.txt"],
)
def test_reviewer_path_targets_redact_sensitive_workspace_names(
    tmp_path: Path,
    relative_path: str,
) -> None:
    facts = build_facts(
        "read_file",
        {"path": relative_path},
        workspace_root=tmp_path,
    )

    request = build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == [
        {
            "operation": "read",
            "scope": "workspace",
            "target": "[redacted_target]",
            "location_status": "redacted",
        }
    ]
    serialized = json.dumps(request, ensure_ascii=False)
    assert relative_path not in serialized


def test_reviewer_path_targets_redact_system_path_and_keep_scope_neutral(
    tmp_path: Path,
) -> None:
    facts = build_facts(
        "read_file",
        {"path": "/etc/passwd"},
        workspace_root=tmp_path,
    )

    request = build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == [
        {
            "operation": "read",
            "scope": "nonworkspace_unclassified",
            "target": "[redacted_target]",
            "location_status": "redacted",
        }
    ]
    serialized = json.dumps(request, ensure_ascii=False)
    assert "/etc/passwd" not in serialized
    assert "passwd" not in serialized


@pytest.mark.parametrize(
    ("external", "policy_level", "expected_scope"),
    [
        (False, "ask", "platform_trusted_root"),
        (True, "ask", "engine_restricted"),
        (True, "deny", "engine_restricted"),
        (True, "allow", "platform_trusted_root"),
    ],
)
def test_engine_restriction_precedes_platform_scope(
    tmp_path: Path,
    external: bool,
    policy_level: str,
    expected_scope: str,
) -> None:
    primary = tmp_path / "project"
    platform = tmp_path / "agent-workspace"
    target = platform / ".env"
    facts = build_facts(
        "read_file",
        {"path": str(target)},
        workspace_root=primary,
        platform_trusted_root=platform,
        external_paths=(str(target),) if external else (),
    )

    request = build_reviewer_action_view(
        facts,
        policy_level=policy_level,
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == [
        {"operation": "read", "scope": expected_scope, "target": "[redacted_target]",
         "location_status": "redacted"}
    ]
    assert str(platform) not in json.dumps(request, ensure_ascii=False)


def test_reviewer_never_resolves_relative_access_against_process_cwd(
    tmp_path: Path,
) -> None:
    facts = replace(
        build_facts(
            "read_file",
            {"path": str(tmp_path / "skills" / "demo" / "SKILL.md")},
            workspace_root=tmp_path,
        ),
        read_paths=("skills/demo/SKILL.md",),
    )

    request = build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == [
        {"operation": "read", "scope": "unknown", "target": "SKILL.md",
         "location_status": "unavailable"}
    ]


def test_reviewer_path_targets_stay_empty_for_unknown_accesses(
    tmp_path: Path,
) -> None:
    facts = build_facts("read_file", {"path": ""}, workspace_root=tmp_path)

    request = build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("manual", "deny"),
        no_auto_allow_reason="accesses_unknown",
        original_user_intent=None,
        domain_route=None,
    ).to_json_dict()

    assert request["review_evidence"]["path_targets"] == []


async def test_auto_reviewer_serializes_concurrent_client_calls(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = ConcurrentTrackingReviewerClient()
    reviewer = AutoReviewer(client=client)

    assessments = await asyncio.gather(
        reviewer.assess(request),
        reviewer.assess(request),
    )

    assert all(
        assessment.outcome == ReviewerOutcome.ALLOW_ONCE for assessment in assessments
    )
    assert client.max_active_calls == 1


async def test_isolated_model_client_uses_fresh_bounded_context(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
        review_evidence={
            "request_context": {
                "request_id": "internal-request",
                "tool_call_id": "internal-tool-call",
            },
            "history": {
                "denied_action_digests": ["sha256:internal-denial"],
                "fallback_reasons": ["semantic-history-marker"],
            },
            "command": {
                "argv_digest": "sha256:internal-command",
                "programs": ["git"],
            },
            "nested": {
                "turn_id": "internal-turn",
                "source_request_id": "internal-source-request",
                "summary": "semantic-evidence-marker",
            },
        },
    )
    model = RecordingIsolatedReviewerModel()
    client = IsolatedModelReviewerClient(model=model)

    await client.assess(request)
    await client.assess(request)

    assert len(model.calls) == 2
    first_messages, first_kwargs = model.calls[0]
    second_messages, second_kwargs = model.calls[1]
    assert first_messages is not second_messages
    assert [message["role"] for message in first_messages] == ["system", "user"]
    assert first_messages == second_messages
    assert first_kwargs == second_kwargs == {"temperature": 0.0, "top_p": 1.0}
    system_prompt = first_messages[0]["content"]
    assert "agent-declared intent" not in system_prompt
    assert "insufficient task alignment" in system_prompt
    assert "unnamed public domain" not in system_prompt
    assert "single task-consistent public HTTPS readonly fetch" not in system_prompt
    assert "sandbox_package_install candidate" not in system_prompt
    assert "not by itself a denial reason" not in system_prompt
    model_payload = json.loads(first_messages[1]["content"])
    assert model_payload["request"]["phase_scope"] == "takeover"
    serialized_model_payload = json.dumps(model_payload, sort_keys=True)
    assert "request_context" not in serialized_model_payload
    assert "request_id" not in serialized_model_payload
    assert "tool_call_id" not in serialized_model_payload
    assert "turn_id" not in serialized_model_payload
    assert "source_request_id" not in serialized_model_payload
    assert "digest" not in serialized_model_payload
    assert "internal-request" not in serialized_model_payload
    assert "internal-tool-call" not in serialized_model_payload
    assert "internal-turn" not in serialized_model_payload
    assert "internal-source-request" not in serialized_model_payload
    assert "internal-command" not in serialized_model_payload
    assert "internal-denial" not in serialized_model_payload
    assert "semantic-evidence-marker" not in serialized_model_payload
    assert "semantic-history-marker" not in serialized_model_payload
    assert '"programs": ["git"]' in serialized_model_payload
    assert "tools" not in first_kwargs
    assert "callbacks" not in first_kwargs
    assert not hasattr(client, "permission_host")
    assert not hasattr(client, "tool_runner")
    assert not hasattr(client, "memory")


async def test_isolated_model_client_uses_latest_configured_display_language(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    model = RecordingIsolatedReviewerModel()
    current_language = ["zh"]
    client = IsolatedModelReviewerClient(
        model=model,
        display_language_getter=lambda: current_language[0],
    )

    await client.assess(request)
    current_language[0] = "en"
    await client.assess(request)

    first_messages, _ = model.calls[0]
    second_messages, _ = model.calls[1]
    assert "Simplified Chinese" in first_messages[0]["content"]
    assert "English" in second_messages[0]["content"]
    for system_message in (first_messages[0]["content"], second_messages[0]["content"]):
        assert "reason_code" in system_message
        assert "stable ASCII machine values" in system_message
    assert first_messages[1] == second_messages[1]


@pytest.mark.parametrize(
    "outcome",
    [ReviewerOutcome.ALLOW_ONCE, ReviewerOutcome.MANUAL, ReviewerOutcome.DENY],
)
async def test_isolated_model_client_accepts_strict_reasoning_only_response(
    tmp_path: Path,
    outcome: str,
) -> None:
    request = _skill_read_request(tmp_path)
    response = _valid_response(request, outcome=outcome)
    client = IsolatedModelReviewerClient(
        model=StaticIsolatedReviewerModel(
            SimpleNamespace(content="", reasoning_content=response)
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == outcome
    assert assessment.fallback_reason == ""


async def test_isolated_model_client_uses_reasoning_for_whitespace_content(
    tmp_path: Path,
) -> None:
    request = _readonly_fetch_request(tmp_path)
    response = _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)
    client = IsolatedModelReviewerClient(
        model=StaticIsolatedReviewerModel(
            SimpleNamespace(content=" \n\t", reasoning_content=response)
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.ALLOW_ONCE
    assert assessment.fallback_reason == ""


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(content="", reasoning_content={"outcome": "allow_once"}),
        SimpleNamespace(content="", reasoning_content="[1, 2]"),
        SimpleNamespace(content="", reasoning_content="```json\n{}\n```"),
        SimpleNamespace(content="", reasoning_content='{} {"outcome":"deny"}'),
        SimpleNamespace(content="", reasoning_content="prefix {} suffix"),
        SimpleNamespace(content="", reasoning_content=""),
    ],
)
async def test_isolated_model_client_rejects_non_strict_reasoning_response(
    tmp_path: Path,
    response: object,
) -> None:
    request = _readonly_fetch_request(tmp_path)
    client = IsolatedModelReviewerClient(model=StaticIsolatedReviewerModel(response))

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "invalid_json"


async def test_isolated_model_client_does_not_override_invalid_nonempty_content(
    tmp_path: Path,
) -> None:
    request = _readonly_fetch_request(tmp_path)
    valid_reasoning = _valid_response(
        request,
        outcome=ReviewerOutcome.ALLOW_ONCE,
    )
    client = IsolatedModelReviewerClient(
        model=StaticIsolatedReviewerModel(
            SimpleNamespace(content="not json", reasoning_content=valid_reasoning)
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "invalid_json"


async def test_isolated_model_client_prefers_valid_content_over_reasoning(
    tmp_path: Path,
) -> None:
    request = _skill_read_request(tmp_path)
    content = _valid_response(request, outcome=ReviewerOutcome.DENY)
    reasoning = _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)
    client = IsolatedModelReviewerClient(
        model=StaticIsolatedReviewerModel(
            SimpleNamespace(content=content, reasoning_content=reasoning)
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.DENY


async def test_isolated_model_client_rejects_non_string_content(
    tmp_path: Path,
) -> None:
    request = _readonly_fetch_request(tmp_path)
    valid_reasoning = _valid_response(
        request,
        outcome=ReviewerOutcome.ALLOW_ONCE,
    )
    client = IsolatedModelReviewerClient(
        model=StaticIsolatedReviewerModel(
            SimpleNamespace(content={"text": ""}, reasoning_content=valid_reasoning)
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "invalid_json"


def test_reviewer_ui_metadata_removes_manual_only_fields_from_approved(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    monkeypatch.setattr(
        reviewer_metadata_module,
        "_configured_reviewer_display_language",
        lambda: "zh",
    )

    metadata = reviewer_metadata_module._reviewer_ui_metadata(
        descriptor,
        reason="reviewer_allow_once",
        metadata={
            "decision_source": "auto_reviewer",
            "final_reviewer_status": "approved",
            "reviewer_outcome": "allow_once",
            "reviewer_reason_summary": "操作符合用户意图。",
            "manual_reason_code": "stale_manual",
            "manual_reason_summary": "需要人工审批。",
            "user_review_hint": "请人工批准。",
            "user_authorization": "unknown",
        },
    )

    assert metadata["evidence_summary"] == "操作符合用户意图。"
    assert "manual_reason_code" not in metadata
    assert "manual_reason_summary" not in metadata
    assert "reviewer_user_review_hint" not in metadata
    assert "user_review_hint" not in metadata
    assert "user_authorization" not in metadata


def test_reviewer_ui_metadata_localizes_unverified_execution_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    monkeypatch.setattr(
        reviewer_metadata_module,
        "_configured_reviewer_display_language",
        lambda: "zh",
    )

    metadata = reviewer_metadata_module._reviewer_ui_metadata(
        descriptor,
        reason="execution_provider_contract_unverified",
        metadata={
            "decision_source": "execution_provider_contract",
            "final_reviewer_status": "manual",
            "reviewer_outcome": "manual",
            "manual_reason_code": "execution_provider_contract_unverified",
        },
    )

    assert "无法验证当前工具的执行 provider 合同" in metadata["manual_reason_summary"]
    assert "人工允许不会改变工具的实际执行环境" in metadata["manual_reason_summary"]
    assert "JiuwenBox" not in metadata["manual_reason_summary"]
    assert "没有沙箱" not in metadata["manual_reason_summary"]


def test_reviewer_ui_metadata_prefers_precise_host_reason_for_downgraded_allow(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    monkeypatch.setattr(
        reviewer_metadata_module,
        "_configured_reviewer_display_language",
        lambda: "zh",
    )

    metadata = reviewer_metadata_module._reviewer_ui_metadata(
        descriptor,
        reason="reviewer_outcome_not_allowed",
        metadata={
            "decision_source": "auto_reviewer",
            "final_reviewer_status": "manual",
            "reviewer_reason_code": "reviewer_outcome_not_allowed",
            "reviewer_reason_summary": "AutoReviewer requested allow_once.",
            "host_manual_reason_code": "original_user_intent_missing",
            "host_manual_reason_summary": (
                "写入目标位于工作区之外，且可信用户意图未明确指定该路径，因此需要人工审批。"
            ),
        },
    )

    assert metadata["manual_reason_code"] == "reviewer_outcome_not_allowed"
    assert metadata["host_manual_reason_code"] == (
        "original_user_intent_missing"
    )
    assert metadata["evidence_summary"] == metadata["host_manual_reason_summary"]
    assert metadata["manual_reason_summary"] == metadata["host_manual_reason_summary"]
    assert "结论超出宿主策略允许范围" not in metadata["manual_reason_summary"]


def test_host_owned_manual_display_uses_current_host_candidate_reason(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _manual_only_candidate(tmp_path)
    metadata, display_reason = (
        reviewer_metadata_module._with_host_owned_manual_review_display(
            descriptor,
            candidate,
            {
                "decision_source": "auto_reviewer",
                "reviewer_outcome": "manual",
                "reviewer_reason_summary": (
                    "The requested destination is not explicit in the user instruction."
                ),
                "manual_reason_summary": (
                    "The requested destination is not explicit in the user instruction."
                ),
                "user_review_hint": "Verify the destination before approving.",
            },
        )
    )

    assert display_reason == metadata["host_manual_reason_summary"]
    assert metadata["manual_reason_summary"] == display_reason
    assert display_reason != (
        "The requested destination is not explicit in the user instruction."
    )
    assert metadata["host_manual_reason_code"] == "original_user_intent_missing"
    assert metadata["user_review_hint"]


def test_reviewer_ui_metadata_localizes_generic_deterministic_reason(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    monkeypatch.setattr(
        reviewer_metadata_module,
        "_configured_reviewer_display_language",
        lambda: "en",
    )

    metadata = reviewer_metadata_module._reviewer_ui_metadata(
        descriptor,
        reason="deterministic_scope",
        metadata={
            "decision_source": "deterministic_sandbox_scope",
            "final_reviewer_status": "deterministic_allow",
            "reviewer_outcome": "allow_once",
            "reviewer_reason_code": "deterministic_scope",
            "evidence_summary": "stale hard-coded text",
        },
    )

    assert metadata["evidence_summary"] == (
        "Host permission rules verified that this operation meets automatic "
        "execution conditions."
    )
    assert "manual_reason_summary" not in metadata
    assert "user_review_hint" not in metadata


def test_reviewer_ui_metadata_localizes_host_fallback_reasons(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    cases = (
        (
            "timed_out",
            "reviewer_fallback",
            "reviewer_timeout",
            "自动审批审查超时",
            "AutoReviewer timed out",
        ),
        (
            "manual",
            "reviewer_fallback",
            "invalid_json",
            "自动审批响应无法通过宿主校验",
            "did not pass host validation",
        ),
        (
            "manual",
            "reviewer_fallback",
            "low_confidence",
            "置信度低于当前阈值",
            "confidence was below",
        ),
        (
            "manual",
            "reviewer_outcome_not_allowed",
            "",
            "结论超出宿主策略允许范围",
            "outside the host policy allowance",
        ),
    )
    for status, reason_code, fallback_reason, zh_text, en_text in cases:
        metadata_input = {
            "decision_source": "auto_reviewer",
            "final_reviewer_status": status,
            "reviewer_reason_code": reason_code,
            "reviewer_reason_summary": fallback_reason or reason_code,
            "fallback_reason": fallback_reason,
        }
        for language, expected in (("zh", zh_text), ("en", en_text)):
            monkeypatch.setattr(
                reviewer_metadata_module,
                "_configured_reviewer_display_language",
                lambda value=language: value,
            )
            metadata = reviewer_metadata_module._reviewer_ui_metadata(
                descriptor,
                reason=reason_code,
                metadata=metadata_input,
            )

            assert expected in metadata["evidence_summary"]
            assert metadata["manual_reason_summary"] == metadata["evidence_summary"]
            if fallback_reason:
                assert fallback_reason not in metadata["evidence_summary"]


def test_reviewer_ui_metadata_terminal_status_drops_historical_manual_fields(
    tmp_path: Path,
) -> None:
    descriptor, _ = _candidate(tmp_path)
    for final_status in ("approved", "denied"):
        metadata = reviewer_metadata_module._reviewer_ui_metadata(
            descriptor,
            reason="terminal",
            metadata={
                "decision_source": "auto_reviewer",
                "final_reviewer_status": final_status,
                "reviewer_outcome": "manual",
                "reviewer_raw_outcome": "manual",
                "manual_reason_code": "historical_manual",
                "manual_reason_summary": "Historical manual reason.",
                "user_review_hint": "Review this manually.",
            },
        )

        assert "manual_reason_code" not in metadata
        assert "manual_reason_summary" not in metadata
        assert "reviewer_user_review_hint" not in metadata
        assert "user_review_hint" not in metadata


async def test_auto_review_request_binds_allowed_outcomes(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _manual_only_candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    allowed_candidate = reviewer_route(
        build_facts(
            "read_file",
            {"path": "README.md"},
            workspace_root=tmp_path,
        ),
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=None,
        domain_route=None,
    )
    allowed_request = _build_request(
        candidate=allowed_candidate,
        policy_reason="policy_ask",
    )

    assert request.allowed_outcomes == ("manual", "deny")
    assert request.no_auto_allow_reason == candidate.no_auto_allow_reason
    assert request.no_auto_allow_reason == "original_user_intent_missing"
    assert request.review_evidence == {}
    assert request.to_json_dict()["allowed_outcomes"] == ["manual", "deny"]
    assert allowed_request.allowed_outcomes == ("allow_once", "manual", "deny")


async def test_auto_reviewer_forces_manual_for_disallowed_allow_once(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _manual_only_candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="exact_exec_shell_control",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.manual_reason_code == "reviewer_outcome_not_allowed"
    assert "allow_once" in assessment.manual_reason_summary


async def test_auto_reviewer_rejects_json_code_fence(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    response = _valid_response(request, outcome=ReviewerOutcome.ALLOW_ONCE)
    client = StaticReviewerClient(f"```json\n{response}\n```")

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.fallback_reason == "invalid_json"


async def test_auto_reviewer_accepts_valid_manual(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome=ReviewerOutcome.MANUAL)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.manual_reason_code == "workspace_read"
    assert assessment.manual_reason_summary == "The path is a workspace-local read."
    assert assessment.fallback_reason == ""
    assert assessment.user_review_hint


async def test_auto_reviewer_preserves_task_relevant_rationale(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(
            request,
            outcome=ReviewerOutcome.ALLOW_ONCE,
            rationale=(
                "Grep for a project name within sandbox workspace. "
                "User intent is task-relevant. Low risk."
            ),
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.reason_summary == (
        "Grep for a project name within sandbox workspace. "
        "User intent is task-relevant. Low risk."
    )


async def test_auto_reviewer_rejects_manual_without_required_fields(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        json.dumps(
            {
                "outcome": ReviewerOutcome.MANUAL,
                "confidence": 0.95,
                "reason_code": "workspace_read",
                "rationale": "The path is a workspace-local read.",
            }
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.fallback_reason == "missing_manual_field"
    assert assessment.manual_reason_code == "reviewer_fallback"


async def test_auto_reviewer_accepts_valid_deny(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome=ReviewerOutcome.DENY)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.DENY
    assert assessment.status == "denied"


async def test_auto_reviewer_keeps_deny_when_allowlist_omits_it(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = replace(
        _build_request(
            candidate=candidate,
            policy_reason="policy_ask",
        ),
        allowed_outcomes=(
            ReviewerOutcome.ALLOW_ONCE,
            ReviewerOutcome.MANUAL,
        ),
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome=ReviewerOutcome.DENY)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.DENY
    assert assessment.status == "denied"
    assert assessment.fallback_reason == ""


async def test_auto_reviewer_rejects_invalid_json(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )

    assessment = await AutoReviewer(client=StaticReviewerClient("{")).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.fallback_reason == "invalid_json"


async def test_auto_reviewer_rejects_missing_fields(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )

    assessment = await AutoReviewer(client=StaticReviewerClient("{}")).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "missing_field"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("outcome", [ReviewerOutcome.ALLOW_ONCE]),
        ("outcome", " "),
        ("confidence", True),
        ("confidence", "0.95"),
        ("confidence", float("nan")),
        ("reason_code", {}),
        ("reason_code", " "),
        ("rationale", []),
        ("rationale", " "),
        ("manual_reason_code", {}),
        ("manual_reason_summary", []),
        ("user_review_hint", " "),
    ),
)
async def test_auto_reviewer_rejects_non_strict_field_values(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(
            request,
            outcome=ReviewerOutcome.ALLOW_ONCE,
            extra={field_name: invalid_value},
        )
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "manual"
    assert assessment.fallback_reason == "invalid_field"


async def test_auto_reviewer_rejects_unknown_outcome(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(_valid_response(request, outcome="run_tools"))

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "unknown_outcome"


async def test_auto_reviewer_rejects_low_confidence(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome="allow_once", confidence=0.1)
    )

    assessment = await AutoReviewer(client=client, min_confidence=0.7).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "low_confidence"


async def test_auto_reviewer_applies_same_valid_response_to_independent_requests(
    tmp_path: Path,
) -> None:
    first_descriptor, first_candidate = _candidate(tmp_path)
    first_request = _build_request(
        candidate=first_candidate,
        policy_reason="policy_ask",
    )
    second_descriptor = build_facts(
        "read_file",
        {"path": str(tmp_path / "SECOND.md")},
        workspace_root=tmp_path,
    )
    second_candidate = reviewer_route(
        second_descriptor,
        policy_level="ask",
        guard_result="not_applicable",
        workspace_root=tmp_path,
        original_user_intent=None,
        domain_route=None,
    )
    assert second_candidate.accepted
    second_request = _build_request(
        candidate=second_candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(first_request, outcome=ReviewerOutcome.ALLOW_ONCE)
    )
    reviewer = AutoReviewer(client=client)

    first_assessment, second_assessment = await asyncio.gather(
        reviewer.assess(first_request),
        reviewer.assess(second_request),
    )

    assert first_assessment.outcome == second_assessment.outcome == "allow_once"


async def test_auto_reviewer_bounds_oversized_rationale(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    rationale = "x" * (AUTO_REVIEW_REASON_SUMMARY_LIMIT * 2)
    client = StaticReviewerClient(
        _valid_response(request, outcome="allow_once", rationale=rationale)
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.ALLOW_ONCE
    assert len(assessment.reason_summary) == AUTO_REVIEW_REASON_SUMMARY_LIMIT
    assert assessment.reason_summary.endswith("...")


async def test_auto_reviewer_rejects_forbidden_privilege_fields(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    forbidden_fields = (
        "grant",
        "allow_always",
        "config_update",
        "permission_override",
        "command_rewrite",
        "tool_calls",
    )

    for field_name in forbidden_fields:
        client = StaticReviewerClient(
            _valid_response(request, outcome="allow_once", extra={field_name: {}})
        )
        assessment = await AutoReviewer(client=client).assess(request)
        assert assessment.outcome == ReviewerOutcome.MANUAL
        assert assessment.fallback_reason == "forbidden_field"


async def test_auto_reviewer_rejects_unknown_fields(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = StaticReviewerClient(
        _valid_response(request, outcome="allow_once", extra={"risk_level": "low"})
    )

    assessment = await AutoReviewer(client=client).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.fallback_reason == "unknown_field"


def test_auto_review_request_is_locked_down_and_sanitized(tmp_path: Path) -> None:
    raw_marker = "raw-future-secret-marker"
    descriptor = build_facts(
        "read_file",
        {
            "path": str(tmp_path / "README.md"),
            "future": {"nested": raw_marker},
        },
        workspace_root=tmp_path,
    )
    action_view = build_reviewer_action_view(
        descriptor,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        original_user_intent=None,
        domain_route=None,
    )

    payload = action_view.to_json_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert raw_marker not in serialized
    assert "future" not in serialized
    assert "untrusted_args" not in payload
    assert not hasattr(action_view, "untrusted_evidence")
    assert not hasattr(action_view, "tool_runner")
    assert not hasattr(action_view, "permission_host")
    assert not hasattr(action_view, "approval_callback")
    assert not hasattr(action_view, "mutable_permission_config")


async def test_auto_reviewer_timeout_falls_back_to_manual(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )
    client = TrackingTimeoutReviewerClient()

    assessment = await AutoReviewer(
        client=client,
        timeout_ms=1,
    ).assess(request)

    assert client.calls == 1
    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "timed_out"
    assert assessment.fallback_reason == "reviewer_timeout"
    assert assessment.manual_reason_code == "reviewer_timeout"
    assert assessment.user_review_hint


async def test_auto_reviewer_exception_falls_back_to_manual(tmp_path: Path) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )

    assessment = await AutoReviewer(client=FailingReviewerClient()).assess(request)

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "aborted"
    assert assessment.fallback_reason == "client_exception"
    assert assessment.manual_reason_code == "reviewer_fallback"
    assert assessment.user_review_hint


async def test_auto_reviewer_unexpected_exception_falls_back_to_manual(
    tmp_path: Path,
) -> None:
    descriptor, candidate = _candidate(tmp_path)
    request = _build_request(
        candidate=candidate,
        policy_reason="policy_ask",
    )

    assessment = await AutoReviewer(client=UnexpectedFailingReviewerClient()).assess(
        request
    )

    assert assessment.outcome == ReviewerOutcome.MANUAL
    assert assessment.status == "aborted"
    assert assessment.fallback_reason == "client_exception"
