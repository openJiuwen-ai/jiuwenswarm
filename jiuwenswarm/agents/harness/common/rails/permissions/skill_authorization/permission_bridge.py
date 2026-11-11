# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project an approved Skill into the existing request permission snapshot."""

from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any

from openjiuwen.harness.rails.skills.skill_lifecycle_events import (
    SKILL_AUTHORIZATION_GATE_HANDLED_KEY,
)
from openjiuwen.harness.security.skill_authorization import (
    compose_skill_permissions,
    get_skill_grant_store,
    is_skill_authorization_enabled,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail,
)
from jiuwenswarm.runtime.run_permissions import overlay_run_permissions

_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar("skill_permission_scope", default=None)


def gate_handled(ctx: Any) -> bool:
    inputs = ctx.inputs
    call_id = str(getattr(inputs.tool_call, "id", "") or "")
    if call_id and ctx.extra.get("_subagent_permission_handled") == call_id:
        return True
    if inputs.tool_name not in {"skill_tool", "skill_complete"}:
        return False
    marker = ctx.extra.get(SKILL_AUTHORIZATION_GATE_HANDLED_KEY)
    return marker is True or bool(call_id and marker == call_id)


def compose_active_permissions(snapshot: Any) -> Any:
    scope = _SCOPE.get()
    if not isinstance(snapshot, dict) or scope is None or not is_skill_authorization_enabled(snapshot):
        return snapshot
    active = get_skill_grant_store().get_active(*scope)
    return compose_skill_permissions(snapshot, active.overlay_snapshot) if active else snapshot


class SkillAuthorizationPermissionRail(JiuwenSwarmPermissionInterruptRail):
    """Keep the current permission rail and its persistence/queue contracts."""

    @property
    def permission_engine(self):
        return self._engine

    def get_permissions_snapshot(self):
        return self._host.get_permissions_snapshot()

    def set_skill_scope_resolver(self, resolver) -> None:
        self._skill_scope_resolver = resolver

    @contextmanager
    def skill_check_context(self, ctx):
        resolver = getattr(self, "_skill_scope_resolver", None)
        token = _SCOPE.set(resolver(ctx) if resolver else None)
        try:
            yield
        finally:
            _SCOPE.reset(token)

    def _invoke_permissions_hook(self, hook, *args, session_id=None):
        snapshot = super()._invoke_permissions_hook(hook, *args, session_id=session_id)
        if hook is self._host.get_permissions_snapshot:
            effective = compose_active_permissions(snapshot)
            return overlay_run_permissions(effective) if isinstance(effective, dict) else effective
        return snapshot

    async def before_tool_call(self, ctx) -> None:
        if gate_handled(ctx):
            return
        with self.skill_check_context(ctx):
            await super().before_tool_call(ctx)
