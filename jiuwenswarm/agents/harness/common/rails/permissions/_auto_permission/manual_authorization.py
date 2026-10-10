"""Migrated Auto Permission manual authorization slice."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.domain_manual import (
    _permission_prompt_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.invocation_context import (
    _normalize_channel_kind,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _localized_host_display_reason,
    _reviewer_ui_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import ASK_LEVEL
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_manual_approval_required_response,
)


class AutoPermissionManualAuthorizationMixin:
    """Phase 8c manual authorization behavior."""

    def _manual_reviewer_response(
        self,
        facts: ToolDecisionFacts,
        *,
        reason: str,
        display_reason: str | None = None,
        metadata: dict[str, object],
        session_id: str | None = None,
        request_id: str | None = None,
        tool_call_id: str | None = None,
        include_prompt_payload: bool = True,
        channel_kind: str | None = None,
    ) -> Any:
        resolved_channel_kind = _normalize_channel_kind(
            channel_kind or metadata.get("channel_kind")
        )
        metadata = {
            **metadata,
            "channel_kind": resolved_channel_kind,
            "record_kind": "permission_reviewer_assessment",
        }
        prompt_payload = None
        if include_prompt_payload:
            prompt_payload = _permission_prompt_payload(
                facts,
                tool_call_id=tool_call_id,
            )
        if include_prompt_payload and session_id:
            if request_id and tool_call_id:
                metadata = {**metadata, "auto_permission_manual": True}
        metadata = _reviewer_ui_metadata(
            facts,
            reason=reason,
            metadata=metadata,
        )
        self._emit_audit(
            facts,
            decision=ASK_LEVEL,
            reason=reason,
            degraded=True,
            extra=metadata,
        )
        visible_reason = ""
        for value in (
            display_reason,
            metadata.get("manual_reason_summary"),
            metadata.get("evidence_summary"),
        ):
            if isinstance(value, str) and value.strip():
                visible_reason = value.strip()
                break
        if not visible_reason:
            visible_reason = _localized_host_display_reason(
                "manual_approval_required",
                metadata,
                reviewer_status="manual",
            )
        return build_manual_approval_required_response(
            visible_reason,
            metadata,
            prompt_payload=prompt_payload,
        )
