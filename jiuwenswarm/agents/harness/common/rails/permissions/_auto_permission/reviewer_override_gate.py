"""Migrated Auto Permission reviewer override gate slice."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _reviewer_route_audit_extra,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_action_summary,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import DENY_LEVEL
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_denied_permission_response,
)


class AutoPermissionReviewerOverrideGateMixin:
    """Phase 8c reviewer override gate behavior."""

    def _allow_once_reviewer_gate_decision(
        self,
        facts: ToolDecisionFacts,
        *,
        policy_level: str,
        original_user_intent: OriginalUserIntentEvidence | None,
        domain_route: DecisionRoute | None,
    ) -> tuple[bool, Any | None]:
        if self.auto_reviewer is None:
            return False, None
        guard_result = "scoped_candidate" if domain_route is not None else "not_applicable"
        route = self._reviewer_route(
            facts,
            policy_level=policy_level,
            guard_result=guard_result,
            original_user_intent=original_user_intent,
            domain_route=domain_route,
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
            return True, build_denied_permission_response(route.reason)
        return True, None
