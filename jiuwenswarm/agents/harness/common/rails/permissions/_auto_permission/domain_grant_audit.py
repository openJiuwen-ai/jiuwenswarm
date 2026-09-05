"""Compact fixed-domain routing and response handling."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.domain_manual import (
    _denied_permission_response,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    PermissionHandlingResult,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_action_summary,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    ASK_LEVEL,
    DENY_LEVEL,
    deterministic_domain_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
)
from jiuwenswarm.agents.harness.common.rails.permissions.sandbox_profile import (
    SandboxDescriptor,
    build_sandbox_profile,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_manual_approval_required_response,
)


class AutoPermissionDomainGrantAuditMixin:
    """Evaluate the fixed route table without plugins or registries."""

    def _evaluate_domain_route(
        self,
        facts: ToolDecisionFacts,
        *,
        original_user_intent: OriginalUserIntentEvidence | None,
        recent_url_sources: tuple[Any, ...] = (),
    ) -> DecisionRoute | None:
        return deterministic_domain_route(
            facts,
            original_user_intent=original_user_intent,
            recent_url_sources=recent_url_sources,
            browser_runtime_security_profile=self.browser_runtime_security_profile,
        )

    def _current_sandbox_profile(self) -> SandboxDescriptor | None:
        if self.sys_operation is None:
            return self.sandbox
        self.sandbox = build_sandbox_profile(self.sys_operation)
        return self.sandbox

    async def _handle_domain_route(
        self,
        facts: ToolDecisionFacts,
        *,
        domain_route: DecisionRoute,
        policy_level: str,
        session_id: str | None,
        request_id: str | None,
        tool_call_id: str | None,
        user_input: Any,
        original_user_intent: OriginalUserIntentEvidence | None,
        channel_kind: str = "web",
        runtime_ctx: Any | None = None,
    ) -> PermissionHandlingResult:
        if domain_route.requires_reviewer:
            reviewer_result = await self._maybe_run_reviewer(
                facts,
                policy_level=policy_level,
                policy_reason=domain_route.reason,
                session_id=session_id,
                request_id=request_id,
                tool_call_id=tool_call_id,
                user_input=user_input,
                original_user_intent=original_user_intent,
                domain_route=domain_route,
                guard_result="scoped_candidate",
                channel_kind=channel_kind,
                runtime_ctx=runtime_ctx,
            )
            if reviewer_result.handled:
                return reviewer_result
        return PermissionHandlingResult(
            True,
            self._manual_domain_route_response(facts, domain_route),
            "domain_policy",
        )

    def _deny_domain_route(
        self,
        facts: ToolDecisionFacts,
        route: DecisionRoute,
    ) -> Any:
        metadata = self._domain_route_metadata(facts, route, final_status="denied")
        self._emit_audit(
            facts,
            decision=DENY_LEVEL,
            reason=route.reason,
            degraded=False,
            extra=metadata,
        )
        return _denied_permission_response(route.reason, metadata)

    def _manual_domain_route_response(
        self,
        facts: ToolDecisionFacts,
        route: DecisionRoute,
    ) -> Any:
        metadata = self._domain_route_metadata(facts, route, final_status="manual")
        self._emit_audit(
            facts,
            decision=ASK_LEVEL,
            reason=route.reason,
            degraded=True,
            extra=metadata,
        )
        return build_manual_approval_required_response(route.reason, metadata)

    @staticmethod
    def _domain_route_metadata(
        facts: ToolDecisionFacts,
        route: DecisionRoute,
        *,
        final_status: str,
    ) -> dict[str, object]:
        return {
            "action_summary": _reviewer_action_summary(facts),
            "decision_source": "host_route",
            "host_route_reason": route.reason,
            "host_route_source": route.source,
            "final_reviewer_status": final_status,
            "hide_always_allow": True,
            "manual_reason_code": route.reason,
            "permission_status": final_status,
            "reviewer_status": final_status,
            "risk_tier": facts.capability.risk_tier,
        }

    def _deny(
        self,
        facts: ToolDecisionFacts,
        reason: str,
        *,
        decision_source: str,
    ) -> Any:
        metadata = {
            "action_summary": _reviewer_action_summary(facts),
            "decision_source": decision_source,
            "final_reviewer_status": "denied",
            "reviewer_status": "denied",
            "risk_tier": facts.capability.risk_tier,
        }
        self._emit_audit(facts, decision=DENY_LEVEL, reason=reason, degraded=False)
        return _denied_permission_response(reason, metadata)
