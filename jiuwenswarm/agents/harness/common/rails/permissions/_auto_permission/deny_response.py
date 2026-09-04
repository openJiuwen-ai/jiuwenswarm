"""Migrated Auto Permission deny response slice."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.domain_manual import (
    _denied_permission_response,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.invocation_context import (
    _normalize_channel_kind,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    _REVIEWER_DENIAL_GUIDANCE,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_ui_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import DENY_LEVEL
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_stream_metadata import (
    record_reviewer_tool_result_metadata,
)


class AutoPermissionDenyResponseMixin:
    def _reviewer_deny_response(
        self,
        facts: ToolDecisionFacts,
        *,
        channel_kind: str,
        audit_extra: dict[str, object],
    ) -> tuple[Any, str]:
        metadata = _reviewer_ui_metadata(
            facts,
            reason="reviewer_deny",
            metadata={
                **audit_extra,
                "channel_kind": _normalize_channel_kind(channel_kind),
            },
        )
        self._emit_audit(
            facts,
            decision=DENY_LEVEL,
            reason="reviewer_deny",
            degraded=False,
            extra=metadata,
        )
        response = _denied_permission_response("reviewer_deny", metadata)
        denial_summary = str(
            metadata.get("reviewer_reason_summary")
            or metadata.get("evidence_summary")
            or ""
        ).strip()
        summary_suffix = f": {denial_summary}" if denial_summary else ""
        response["feedback"] = (
            f"[PERMISSION_DENIED] reviewer_deny{summary_suffix}. "
            f"{_REVIEWER_DENIAL_GUIDANCE}"
        )
        return response, "auto_reviewer"

    @staticmethod
    def _record_reviewer_success_metadata(
        ctx: Any | None,
        facts: ToolDecisionFacts,
        *,
        tool_call_id: str | None,
        reason: str,
        metadata: Mapping[str, object],
    ) -> None:
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, MutableMapping):
            return
        reviewer_metadata = _reviewer_ui_metadata(
            facts,
            reason=reason,
            metadata=metadata,
        )
        record_reviewer_tool_result_metadata(
            extra,
            tool_call_id=tool_call_id,
            metadata=reviewer_metadata,
        )
