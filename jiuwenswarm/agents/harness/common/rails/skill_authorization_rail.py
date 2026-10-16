# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expose the skill gate's scope resolution to the permission rail."""

import portalocker
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.skills.skill_authorization_rail import (
    SkillAuthorizationRail as CoreSkillAuthorizationRail,
)


class SkillAuthorizationRail(CoreSkillAuthorizationRail):
    """Skill gate with a public scope resolver for host integration."""

    def resolve_scope(self, ctx: AgentCallbackContext) -> tuple[str, str] | None:
        """Reuse the gate's scope resolution without changing authorization rules."""
        return self._resolve_scope(ctx)

    def set_sessions_root(self, sessions_root) -> None:
        """Carry trusted tenant identity across supervisor/checkpoint tasks."""
        self._sessions_root = sessions_root

    def _bind_session_files(self, ctx, *, sync_policy: bool = True) -> None:
        from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
            bind_session_file_context,
        )

        scope = self.resolve_scope(ctx)
        root = getattr(self, "_sessions_root", None)
        if scope is not None and root is not None:
            bind_session_file_context(scope[0], root)
            sync = getattr(self.store, "sync_feature_state", None)
            enabled = self._feature_enabled()
            # Rebind identity after the tool, without a second unchanged-policy
            # transaction. A policy disabled during the call still retires it.
            if callable(sync) and (sync_policy or not enabled):
                try:
                    sync(scope[0], enabled=enabled)
                except (
                    OSError, ValueError, TypeError, RuntimeError,
                    portalocker.exceptions.LockException,
                ) as exc:
                    raise AbortError(
                        "Skill authorization storage unavailable; retry the request"
                    ) from exc

    async def before_tool_call(self, ctx):
        self._bind_session_files(ctx)
        return await super().before_tool_call(ctx)

    async def after_tool_call(self, ctx):
        self._bind_session_files(ctx, sync_policy=False)
        return await super().after_tool_call(ctx)
