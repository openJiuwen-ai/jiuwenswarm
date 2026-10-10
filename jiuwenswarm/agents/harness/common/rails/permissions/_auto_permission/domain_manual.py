"""Compact permission response helpers shared by manual and deny paths."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)


def _denied_permission_response(
    reason: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    reason_text = str(reason or "permission_denied").strip() or "permission_denied"
    reviewer_metadata = dict(metadata)
    return {
        "approved": False,
        "auto_confirm": False,
        "feedback": f"[PERMISSION_DENIED] {reason_text}",
        "reason": reason_text,
        "success": False,
        "status": "denied",
        "permission_decision": "deny",
        "permission_status": "denied",
        "reviewer_metadata": reviewer_metadata,
    }


def _permission_prompt_payload(
    facts: ToolDecisionFacts,
    *,
    tool_call_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_name": facts.tool_name,
        "tool_args": dict(facts.untrusted_args),
    }
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload
