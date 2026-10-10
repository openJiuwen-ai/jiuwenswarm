"""Migrated Auto Permission deterministic slice."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    DETERMINISTIC_BOUNDED_SCOPE_DECISION_SOURCE,
    DETERMINISTIC_READONLY_PUBLIC_WEB_REASON,
    PermissionHandlingResult,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_action_summary,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _reviewer_route_audit_extra,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    ALLOW_LEVEL,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    ReviewerOutcome,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_route import (
    reviewer_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)


class AutoPermissionDeterministicMixin:
    """Phase 8c deterministic behavior."""

    async def _maybe_allow_deterministic_sandbox_scope(
        self,
        facts: ToolDecisionFacts,
        *,
        route: DecisionRoute,
        policy_level: str,
        session_id: str | None,
        tool_call_id: str | None,
        runtime_ctx: Any | None,
        domain_route: DecisionRoute | None,
        channel_kind: str,
    ) -> PermissionHandlingResult:
        deterministic_reason = (
            DETERMINISTIC_READONLY_PUBLIC_WEB_REASON
            if route.is_deterministic_allow
            else ""
        )
        if not deterministic_reason:
            return PermissionHandlingResult(False, None, "")
        if domain_route is not None:
            return PermissionHandlingResult(False, None, "")

        decision_source = DETERMINISTIC_BOUNDED_SCOPE_DECISION_SOURCE
        evidence_summary = (
            "Bounded public web evidence proved an exact recent-search fetch."
        )
        reviewer_reason_summary = (
            "Readonly public web access is allowed by host deterministic "
            "bounded-scope checks."
        )

        metadata = {
            "action_summary": _reviewer_action_summary(facts),
            "channel_kind": channel_kind,
            "decision_source": decision_source,
            "deterministic_public_web_fetch_bypassed_reviewer": True,
            "evidence_summary": evidence_summary,
            "final_reviewer_status": "deterministic_allow",
            "policy_level": policy_level,
            "reviewer_lifecycle": "not_called",
            "reviewer_outcome": ReviewerOutcome.ALLOW_ONCE,
            "reviewer_reason_code": deterministic_reason,
            "reviewer_reason_summary": reviewer_reason_summary,
            "reviewer_status": "deterministic_allow",
            **_reviewer_route_audit_extra(
                route,
                policy_level=policy_level,
                reviewer_called=False,
            ),
        }
        self._record_reviewer_success_metadata(
            runtime_ctx,
            facts,
            tool_call_id=tool_call_id,
            reason=deterministic_reason,
            metadata=metadata,
        )
        self._emit_audit(
            facts,
            decision=ALLOW_LEVEL,
            reason=deterministic_reason,
            degraded=False,
            extra=metadata,
        )
        return PermissionHandlingResult(True, None, decision_source)

    def _reviewer_route(
        self,
        facts: ToolDecisionFacts,
        *,
        policy_level: str,
        guard_result: str,
        original_user_intent: OriginalUserIntentEvidence | None,
        domain_route: DecisionRoute | None = None,
        recent_url_sources: tuple[Any, ...] = (),
    ) -> DecisionRoute:
        """Return the compact route proven equivalent to the legacy candidate gate."""

        return reviewer_route(
            facts,
            policy_level=policy_level,
            guard_result=guard_result,
            workspace_root=self.workspace_root,
            delivery_max_files=int(
                self.auto_options.get("bounded_write_max_files", 3)
            ),
            delivery_excluded_paths=tuple(
                self.auto_options.get("bounded_write_excluded_paths", ())
            ),
            original_user_intent=original_user_intent,
            domain_route=domain_route,
            recent_url_sources=recent_url_sources,
        )
