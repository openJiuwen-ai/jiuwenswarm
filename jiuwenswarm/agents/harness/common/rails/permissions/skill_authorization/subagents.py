# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attach dev-stable delegated authorization to the SDK child-agent factory."""

from functools import wraps

from openjiuwen.harness.security.skill_authorization import get_subagent_approval_registry

from jiuwenswarm.agents.harness.common.rails.leader_exclusive_tools import EXCLUDED_TOOLS_SPAWN
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
    bind_session_file_context,
)

from jiuwenswarm.enterprise import is_enterprise


def install_subagent_authorization(parent, get_gate):
    """Wrap only the owning parent; each invocation gets a fresh child scope."""
    if not is_enterprise():
        return
    original = parent.create_subagent
    if getattr(original, "skill_authorization_wrapped", False):
        return

    @wraps(original)
    def create(subagent_type, subsession_id, *args, **kwargs):
        child = original(subagent_type, subsession_id, *args, **kwargs)
        gate = get_gate()
        if gate is None or not gate.feature_enabled():
            return child
        if getattr(child.invoke, "skill_authorization_scope", None) is not None:
            raise ValueError("Skill authorization requires a separate child instance per invocation")
        session_id, scope = gate.session_id, str(subsession_id)
        if not session_id or not scope or scope == "main":
            raise ValueError("Child authorization requires an explicit parent session and child scope")
        root = gate.sessions_root
        workspace = getattr(child.deep_config.workspace, "root_path", child.deep_config.workspace)
        registry = get_subagent_approval_registry()
        child_gate, permission = gate.build_child_rails(scope, workspace, registry)
        child.add_rail(child_gate).add_rail(permission)
        # Filter at construction and also enforce at execution in the child rail.
        child.deep_config.tools = [
            tool for tool in (child.deep_config.tools or [])
            if getattr(getattr(tool, "card", tool), "name", "") not in EXCLUDED_TOOLS_SPAWN
        ]

        def cleanup():
            bind_session_file_context(session_id, root)
            registry.cancel_scope(session_id, scope)
            gate.store.clear_scope(session_id, scope)

        invoke, stream = child.invoke, child.stream

        @wraps(invoke)
        async def invoke_with_cleanup(*call_args, **call_kwargs):
            try:
                return await invoke(*call_args, **call_kwargs)
            finally:
                cleanup()

        @wraps(stream)
        async def stream_with_cleanup(*call_args, **call_kwargs):
            try:
                async for chunk in stream(*call_args, **call_kwargs):
                    yield chunk
            finally:
                cleanup()

        invoke_with_cleanup.skill_authorization_scope = scope
        child.invoke, child.stream = invoke_with_cleanup, stream_with_cleanup
        return child

    create.skill_authorization_wrapped = True
    parent.create_subagent = create


def resolve_subagent_approval(*, session_id, request_id, params):
    """Resolve one pending request only within the authenticated parent session."""
    params = params if isinstance(params, dict) else {}
    registry = get_subagent_approval_registry()
    for pending in registry.pending_requests():
        if pending.approval_id != request_id or pending.session_id != session_id:
            continue
        source = f"subagent_{pending.kind.value}"
        if params.get("source", source) != source:
            return False
        scope = params.get("agent_scope_id", pending.agent_scope_id)
        return registry.resolve(
            session_id=session_id, approval_id=request_id, kind=pending.kind,
            agent_scope_id=scope, answer=params.get("answers", []),
        )
    return False
