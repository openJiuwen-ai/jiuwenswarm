"""Consume one exact RootPermissionQueue Auto manual approval."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    PermissionHandlingResult,
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _parse_confirmation_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.execution_provider_contract import (
    requires_manual_execution_provider_review,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    current_root_decision_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    root_permission_resume_from_context,
)


class AutoPermissionReviewerOverrideConsumeMixin:
    """Consume only an Auto-owned pending card already admitted by the queue."""

    async def _consume_reviewer_override(
        self,
        facts: ToolDecisionFacts,
        *,
        invocation: ToolInvocation,
        user_input: Any,
        domain_route: DecisionRoute | None,
    ) -> PermissionHandlingResult:
        payload = _parse_confirmation_payload(user_input)
        if not payload or payload.get("approved") is not True:
            return PermissionHandlingResult(False, None, "")
        resume = root_permission_resume_from_context(invocation.ctx)
        if resume is None or not resume.card.auto_manual:
            return PermissionHandlingResult(False, None, "")
        if payload.get("auto_confirm") is True and self._session_auto_confirm_eligible(
            facts,
            invocation=invocation,
            domain_route=domain_route,
        ):
            key = self._runtime_auto_confirm_key(invocation)
            store = getattr(self.base_rail, "_store_auto_confirm", None)
            if key and callable(store):
                store(invocation.ctx, key)
        self._emit_audit(
            facts,
            decision="allow",
            reason="root_permission_card_approved",
            degraded=False,
            extra={"decision_source": "manual_approval"},
        )
        return PermissionHandlingResult(True, None, "manual_approval")

    def _consume_session_auto_confirm(
        self,
        facts: ToolDecisionFacts,
        *,
        invocation: ToolInvocation,
        user_input: Any,
        domain_route: DecisionRoute | None,
    ) -> PermissionHandlingResult:
        if user_input is not None or not self._session_auto_confirm_eligible(
            facts,
            invocation=invocation,
            domain_route=domain_route,
        ):
            return PermissionHandlingResult(False, None, "")
        key = self._runtime_auto_confirm_key(invocation)
        session = getattr(invocation.ctx, "session", None)
        if not key or session is None:
            return PermissionHandlingResult(False, None, "")
        config = session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
        is_confirmed = getattr(self.base_rail, "_is_auto_confirmed", None)
        if not callable(is_confirmed) or not is_confirmed(config, key):
            return PermissionHandlingResult(False, None, "")
        self._emit_audit(
            facts,
            decision="allow",
            reason="session_auto_confirm",
            degraded=False,
            extra={"decision_source": "session_auto_confirm"},
        )
        return PermissionHandlingResult(True, None, "session_auto_confirm")

    def _session_auto_confirm_eligible(
        self,
        facts: ToolDecisionFacts,
        *,
        invocation: ToolInvocation,
        domain_route: DecisionRoute | None,
    ) -> bool:
        context = current_root_decision_context()
        return bool(
            getattr(invocation.ctx, "session", None) is not None
            and "external_send" not in facts.capability.static_side_effects
            and facts.arguments_valid_object
            and facts.capability.facts_source == "host_static"
            and not facts.capability.alias_conflict
            and (
                facts.capability.category not in {"path", "shell"}
                or facts.accesses_known
            )
            and not requires_manual_execution_provider_review(
                facts.tool_name,
                tool_category=facts.tool_category,
            )
            and not (domain_route is not None and domain_route.requires_manual)
            and not (context is not None and context.auto_review_block_reason)
            and self._runtime_auto_confirm_key(invocation)
        )
