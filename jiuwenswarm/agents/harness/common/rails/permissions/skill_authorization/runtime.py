# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime hooks kept outside legacy session and adapter implementations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        get_effective_permissions_config,
    )

    from openjiuwen.harness.security.skill_authorization import (
        is_skill_authorization_enabled,
    )

    return is_skill_authorization_enabled(get_effective_permissions_config())


def revoke_main_scope(session_id: str | None, *, reason: str) -> None:
    """Deactivate main-scope grants and delegated approvals, best-effort."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    try:
        if not _enabled():
            return
        from openjiuwen.harness.security.skill_authorization import get_skill_grant_store
        from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
            get_subagent_approval_registry,
        )

        get_skill_grant_store().revoke_grants(sid, "main", reason=reason)
        get_subagent_approval_registry().cancel_session(sid)
    except Exception as exc:  # noqa: BLE001 — never mask cancel/error handling
        logger.warning(
            "[skill_authorization] main scope cleanup failed "
            "session_id=%s reason=%s error=%s",
            sid,
            reason,
            exc,
        )


def clear_deleted_session(session_id: str) -> None:
    """Clear existing in-memory state without initializing it while disabled."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    from openjiuwen.harness.security.skill_authorization.grant_store import (
        peek_skill_grant_store,
    )
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalRegistry,
    )

    registry = SubagentApprovalRegistry.peek_instance()
    if registry is not None:
        registry.cancel_session(sid)
    store = peek_skill_grant_store()
    if store is not None:
        store.clear_session(sid)


def revoke_deactivated_main_skill(session_id: str, skill_name: str) -> None:
    """Revoke only the main ACTIVE grant matching Compliance state."""
    sid = str(session_id or "").strip()
    name = str(skill_name or "").strip()
    if not sid or not name:
        return
    try:
        if not _enabled():
            return
        from openjiuwen.harness.security.skill_authorization import get_skill_grant_store

        store = get_skill_grant_store()
        active = store.get_active(sid, "main")
        if active is None or active.skill_name != name:
            return
        store.invalidate_scope(sid, "main", reason="compliance_auto_deactivate")
        store.revoke_grants(sid, "main", reason="compliance_auto_deactivate")
    except Exception as exc:  # noqa: BLE001 — Compliance still deactivates its state
        logger.warning(
            "[skill_authorization] compliance cleanup failed "
            "session_id=%s skill=%s error=%s",
            sid,
            name,
            exc,
        )


def resolve_subagent_approval(
    *,
    request_id: str,
    session_id: str,
    source: str,
    answers: Any,
    agent_scope_id: str | None = None,
) -> bool:
    """Route a delegated answer by explicit source or legacy request prefix."""
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalKind,
        get_subagent_approval_registry,
    )

    if source == "subagent_skill_load" or request_id.startswith("subagent_skill_load_"):
        kind = SubagentApprovalKind.SKILL_LOAD
    elif (
        source == "subagent_tool_permission"
        or request_id.startswith("subagent_tool_permission_")
    ):
        kind = SubagentApprovalKind.TOOL_PERMISSION
    else:
        return False
    registry = get_subagent_approval_registry()
    scope = str(agent_scope_id or "").strip()
    # Relay/OfficeClaw resume often omits agent_scope_id; fill from pending when
    # the approval_id uniquely identifies the wait (older registry builds still
    # require a non-empty scope).
    if not scope:
        for pending in registry.pending_requests():
            if (
                pending.approval_id == request_id
                and pending.session_id == str(session_id or "").strip()
                and pending.kind == kind
            ):
                scope = pending.agent_scope_id
                break
    ok = registry.resolve(
        session_id=session_id,
        approval_id=request_id,
        kind=kind,
        answer=answers,
        agent_scope_id=scope,
    )
    if not ok:
        logger.warning(
            "[skill_authorization] subagent approval resolve miss "
            "session=%s approval_id=%s kind=%s scope=%s pending=%s",
            session_id,
            request_id,
            kind.value,
            scope or "<empty>",
            [p.approval_id for p in registry.pending_requests()],
        )
    return ok
