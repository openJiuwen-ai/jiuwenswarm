"""Migrated Auto Permission reviewer execution slice."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    PermissionHandlingResult,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _is_allow_once_confirmation,
    _reviewer_route_audit_extra,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_action_summary,
    _with_host_owned_manual_review_display,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import DENY_LEVEL
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    ReviewerOutcome,
    build_reviewer_action_view,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    current_root_decision_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.execution_provider_contract import (
    EXECUTION_PROVIDER_CONTRACT_UNVERIFIED,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_denied_permission_response,
)


class AutoPermissionReviewerExecutionMixin:
    """Phase 8c reviewer execution behavior."""

    async def _maybe_run_reviewer(
        self,
        facts: ToolDecisionFacts,
        *,
        policy_level: str,
        policy_reason: str,
        policy_source: str = "permission_engine",
        session_id: str | None,
        request_id: str | None,
        tool_call_id: str | None,
        user_input: Any,
        original_user_intent: OriginalUserIntentEvidence | None,
        domain_route: DecisionRoute | None,
        guard_result: str = "not_applicable",
        channel_kind: str = "web",
        runtime_ctx: Any | None = None,
    ) -> PermissionHandlingResult:
        recent_url_sources = self._recent_url_sources(session_id)
        route = self._reviewer_route(
            facts,
            policy_level=policy_level,
            guard_result=guard_result,
            original_user_intent=original_user_intent,
            domain_route=domain_route,
            recent_url_sources=recent_url_sources,
        )
        if route.is_hard_block:
            metadata = {
                "action_summary": _reviewer_action_summary(facts),
                **_reviewer_route_audit_extra(
                    route,
                    policy_level=policy_level,
                    reviewer_called=False,
                ),
            }
            self._emit_audit(
                facts,
                decision=DENY_LEVEL,
                reason=route.reason,
                degraded=False,
                extra=metadata,
            )
            return PermissionHandlingResult(
                True,
                build_denied_permission_response(route.reason),
                "hard_guard",
            )
        manual_handoff: tuple[str, str, str] | None = None
        if policy_source == "fail_closed":
            manual_handoff = (
                policy_reason,
                "policy_evaluator",
                "The permission policy result is unavailable or malformed; "
                "review this invocation manually.",
            )
        elif route.no_auto_allow_reason == EXECUTION_PROVIDER_CONTRACT_UNVERIFIED:
            manual_handoff = (
                EXECUTION_PROVIDER_CONTRACT_UNVERIFIED,
                "execution_provider_contract",
                "Host cannot verify this tool's execution-provider contract; "
                "review this invocation manually.",
            )
        else:
            execution_context = current_root_decision_context()
            auto_review_block_reason = (
                execution_context.auto_review_block_reason
                if execution_context is not None
                else ""
            )
            if auto_review_block_reason:
                manual_handoff = (
                    auto_review_block_reason,
                    "intent_contract",
                    "The trusted user-intent window exceeds the automatic "
                    "review capacity; review this invocation manually.",
                )
        if manual_handoff is not None:
            manual_reason, manual_source, manual_summary = manual_handoff
            metadata = {
                "action_summary": _reviewer_action_summary(facts),
                "decision_source": manual_source,
                "final_reviewer_status": "manual",
                "manual_reason_code": manual_reason,
                "manual_reason_summary": manual_summary,
                "reviewer_called": False,
                "reviewer_status": "manual",
                **_reviewer_route_audit_extra(
                    route,
                    policy_level=policy_level,
                    reviewer_called=False,
                ),
            }
            return PermissionHandlingResult(
                True,
                self._manual_reviewer_response(
                    facts,
                    reason=manual_reason,
                    metadata=metadata,
                    session_id=session_id,
                    request_id=request_id,
                    tool_call_id=tool_call_id,
                    channel_kind=channel_kind,
                ),
                manual_source,
            )
        deterministic_result: PermissionHandlingResult = (
            await self._maybe_allow_deterministic_sandbox_scope(
                facts,
                route=route,
                policy_level=policy_level,
                session_id=session_id,
                tool_call_id=tool_call_id,
                runtime_ctx=runtime_ctx,
                domain_route=domain_route,
                channel_kind=channel_kind,
            )
        )
        if deterministic_result.handled:
            return deterministic_result
        if self.auto_reviewer is None:
            metadata = {
                "decision_source": "reviewer_unavailable",
                **_reviewer_route_audit_extra(
                    route,
                    policy_level=policy_level,
                    reviewer_called=False,
                ),
                "reviewer_status": "fallback",
            }
            if _is_allow_once_confirmation(user_input):
                authorization_error = self._issue_send_file_authorization(facts)
                if authorization_error:
                    return PermissionHandlingResult(
                        True,
                        self._manual_reviewer_response(
                            facts,
                            reason=authorization_error,
                            metadata={
                                **metadata,
                                "decision_source": "send_file_authorization",
                                "final_reviewer_status": "manual",
                                "manual_reason_code": authorization_error,
                                "reviewer_status": "manual",
                            },
                            session_id=session_id,
                            request_id=request_id,
                            tool_call_id=tool_call_id,
                            channel_kind=channel_kind,
                        ),
                        "send_file_authorization",
                    )
                self._record_reviewer_success_metadata(
                    runtime_ctx,
                    facts,
                    tool_call_id=tool_call_id,
                    reason="user_allow_once_after_reviewer_unavailable",
                    metadata=metadata,
                )
                self._emit_audit(
                    facts,
                    decision="allow",
                    reason="user_allow_once_after_reviewer_unavailable",
                    degraded=False,
                    extra=metadata,
                )
                return PermissionHandlingResult(True, None, "manual_approval")
            return PermissionHandlingResult(
                True,
                self._manual_reviewer_response(
                    facts,
                    reason="reviewer_unavailable",
                    metadata=metadata,
                    session_id=session_id,
                    request_id=request_id,
                    tool_call_id=tool_call_id,
                    channel_kind=channel_kind,
                ),
                "reviewer_unavailable",
            )

        request = build_reviewer_action_view(
            facts,
            policy_level=policy_level,
            policy_reason=policy_reason,
            allowed_outcomes=route.allowed_outcomes,
            no_auto_allow_reason=route.no_auto_allow_reason,
            original_user_intent=original_user_intent,
            domain_route=domain_route,
        )
        assessment = await self.auto_reviewer.assess(request)
        audit_extra = {
            "action_summary": _reviewer_action_summary(facts),
            "channel_kind": channel_kind,
            "decision_source": "auto_reviewer",
            "evidence_summary": assessment.reason_summary,
            "final_reviewer_status": assessment.status,
            "policy_level": policy_level,
            "reviewer_called": True,
            **_reviewer_route_audit_extra(
                route,
                policy_level=policy_level,
                reviewer_called=True,
            ),
            "reviewer_lifecycle": assessment.status,
            "reviewer_outcome": assessment.outcome,
            "reviewer_reason_code": assessment.reason_code,
            "reviewer_reason_summary": assessment.reason_summary,
            "user_review_hint": assessment.user_review_hint,
        }
        if assessment.fallback_reason:
            audit_extra["reviewer_fallback_reason"] = assessment.fallback_reason
        audit_extra["record_kind"] = "permission_reviewer_assessment"
        audit_extra["decision_source"] = "auto_reviewer"

        if assessment.outcome == ReviewerOutcome.ALLOW_ONCE:
            audit_extra = {
                **audit_extra,
                **_reviewer_route_audit_extra(
                    route,
                    policy_level=policy_level,
                    reviewer_called=True,
                ),
                "action_summary": _reviewer_action_summary(facts),
            }
            authorization_error = self._issue_send_file_authorization(facts)
            if authorization_error:
                return PermissionHandlingResult(
                    True,
                    self._manual_reviewer_response(
                        facts,
                        reason=authorization_error,
                        metadata={
                            **audit_extra,
                            "decision_source": "send_file_authorization",
                            "final_reviewer_status": "manual",
                            "manual_reason_code": authorization_error,
                            "reviewer_status": "manual",
                        },
                        session_id=session_id,
                        request_id=request_id,
                        tool_call_id=tool_call_id,
                        channel_kind=channel_kind,
                    ),
                    "send_file_authorization",
                )
            self._record_reviewer_success_metadata(
                runtime_ctx,
                facts,
                tool_call_id=tool_call_id,
                reason="reviewer_allow_once",
                metadata=audit_extra,
            )
            self._emit_audit(
                facts,
                decision="allow",
                reason="reviewer_allow_once",
                degraded=False,
                extra=audit_extra,
            )
            decision_source = (
                "manual_approval"
                if _is_allow_once_confirmation(user_input)
                else "auto_reviewer"
            )
            return PermissionHandlingResult(True, None, decision_source)
        if assessment.outcome == ReviewerOutcome.DENY:
            response, decision_source = self._reviewer_deny_response(
                facts,
                channel_kind=channel_kind,
                audit_extra=audit_extra,
            )
            return PermissionHandlingResult(
                True,
                response,
                decision_source,
            )
        if _is_allow_once_confirmation(user_input):
            authorization_error = self._issue_send_file_authorization(facts)
            if authorization_error:
                return PermissionHandlingResult(
                    True,
                    self._manual_reviewer_response(
                        facts,
                        reason=authorization_error,
                        metadata={
                            **audit_extra,
                            "decision_source": "send_file_authorization",
                            "final_reviewer_status": "manual",
                            "manual_reason_code": authorization_error,
                            "reviewer_status": "manual",
                        },
                        session_id=session_id,
                        request_id=request_id,
                        tool_call_id=tool_call_id,
                        channel_kind=channel_kind,
                    ),
                    "send_file_authorization",
                )
            self._record_reviewer_success_metadata(
                runtime_ctx,
                facts,
                tool_call_id=tool_call_id,
                reason="user_allow_once_after_reviewer_manual",
                metadata=audit_extra,
            )
            self._emit_audit(
                facts,
                decision="allow",
                reason="user_allow_once_after_reviewer_manual",
                degraded=False,
                extra=audit_extra,
            )
            return PermissionHandlingResult(True, None, "manual_approval")
        manual_metadata = {
            **audit_extra,
            "manual_reason_code": (
                assessment.manual_reason_code
                or assessment.reason_code
                or "reviewer_manual"
            ),
            "manual_reason_summary": (
                assessment.manual_reason_summary or assessment.reason_summary
            ),
            "user_review_hint": assessment.user_review_hint,
        }
        manual_metadata, display_reason = _with_host_owned_manual_review_display(
            facts,
            route,
            manual_metadata,
        )
        return PermissionHandlingResult(
            True,
            self._manual_reviewer_response(
                facts,
                reason=(
                    assessment.fallback_reason
                    or assessment.reason_summary
                    or "reviewer_manual"
                ),
                display_reason=display_reason,
                metadata=manual_metadata,
                session_id=session_id,
                request_id=request_id,
                tool_call_id=tool_call_id,
                channel_kind=channel_kind,
            ),
            "auto_reviewer",
        )
