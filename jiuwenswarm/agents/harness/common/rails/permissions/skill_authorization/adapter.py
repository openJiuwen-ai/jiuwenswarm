# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retire persisted Skill grants within the owning tenant/session."""

from agent_gateway.common.protocol_ids import is_valid_session_id

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
    FileSkillGrantStore,
)
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.enterprise import is_enterprise


def existing_session_store(session_id, *, sessions_root=None):
    if not is_valid_session_id(session_id):
        return None
    root = sessions_root or get_agent_sessions_dir()
    store = FileSkillGrantStore(sessions_root=lambda: root)
    session = store.session_directory(session_id)
    if not (session / ".skill_authorization" / "state.json").is_file():
        return None
    return store


def revoke_skill_authorization(session_id, *, reason, sessions_root=None):
    """Task cancellation retires only this tenant/session's temporary grants."""
    if not is_enterprise():
        return
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalRegistry,
    )

    registry = SubagentApprovalRegistry.peek_instance()
    if registry is not None:
        registry.cancel_session(session_id)
    store = existing_session_store(session_id, sessions_root=sessions_root)
    if store is None:
        return

    def retire(grants, document):
        grants.invalidate_scope(session_id, "main", reason=reason)
        grants.revoke_grants(session_id, "main", reason=reason)

    store.operate(session_id, retire)


def clear_deleted_skill_authorization(session_id, *, sessions_root):
    """Tombstone only the validated tenant/session before directory removal."""
    if not is_enterprise():
        return
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalRegistry,
    )

    registry = SubagentApprovalRegistry.peek_instance()
    if registry is not None:
        registry.cancel_session(session_id)
    store = FileSkillGrantStore(sessions_root=lambda: sessions_root)
    if (store.session_directory(session_id) / ".skill_authorization" / "state.json").is_file():
        store.clear_session(session_id)
