"""Migrated Smart Approval reviewer audit helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    ALLOW_LEVEL,
    post_policy_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)

_RETIRED_TASK_SCOPE_CONFIRMATION_KEYS = frozenset(
    {"task_grant", "task_grant_proposal_id", "task_grant_request_id"}
)


def _reviewer_route_audit_extra(
    route: Any,
    *,
    policy_level: str = "",
    reviewer_called: bool | None = None,
) -> dict[str, object]:
    """Project only irreducible Host route provenance into audit metadata."""

    extra: dict[str, object] = {
        "host_route_reason": route.reason,
        "host_route_source": route.source,
    }
    if policy_level:
        extra["policy_level"] = policy_level
    if reviewer_called is not None:
        extra["reviewer_called"] = reviewer_called
    return extra


def _should_degrade_auto_confirm(facts: ToolDecisionFacts, user_input: Any) -> bool:
    payload = _parse_confirmation_payload(user_input)
    if not payload:
        return False
    if payload.get("approved") is not True or payload.get("auto_confirm") is not True:
        return False
    return post_policy_route(facts, policy_level=ALLOW_LEVEL) is not None


def _is_allow_once_confirmation(user_input: Any) -> bool:
    payload = _parse_confirmation_payload(user_input)
    if not payload:
        return False
    if payload.get("approved") is not True:
        return False
    if payload.get("auto_confirm") is True or payload.get("persist_allow") is True:
        return False
    return not (
        _contains_retired_task_scope_confirmation(payload)
        or "reviewer_override_token" in payload
    )


def _contains_retired_task_scope_confirmation(payload: Mapping[str, Any]) -> bool:
    """Recognize retired task-scope payloads so they cannot become allow-once."""

    return any(key in payload for key in _RETIRED_TASK_SCOPE_CONFIRMATION_KEYS)


def _is_rejection_confirmation(user_input: Any) -> bool:
    payload = _parse_confirmation_payload(user_input)
    if not payload:
        return False
    return payload.get("approved") is False


def _parse_confirmation_payload(user_input: Any) -> dict[str, Any] | None:
    if isinstance(user_input, dict):
        return dict(user_input)
    if isinstance(user_input, str):
        try:
            decoded = json.loads(user_input)
        except json.JSONDecodeError:
            return None
        return dict(decoded) if isinstance(decoded, dict) else None
    approved = getattr(user_input, "approved", None)
    if approved is None:
        return None
    return {
        "approved": approved,
        "auto_confirm": getattr(user_input, "auto_confirm", False),
        "persist_allow": getattr(user_input, "persist_allow", False),
        "feedback": getattr(user_input, "feedback", ""),
    }
