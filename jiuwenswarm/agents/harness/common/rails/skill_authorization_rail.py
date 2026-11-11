# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expose the skill gate's scope resolution to the permission rail."""

import portalocker
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.foundation.tool.function.function import LocalFunction
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.skills.skill_authorization_rail import (
    SkillAuthorizationRail as CoreSkillAuthorizationRail,
)


class SkillAuthorizationRail(CoreSkillAuthorizationRail):
    """Skill gate with a public scope resolver for host integration."""

    inherit_to_subagents = False

    def init(self, agent) -> None:
        existing = agent.ability_manager.get("skill_complete")
        if existing is not None and existing is getattr(self, "_complete_card", None):
            return
        if not self._feature_enabled() or agent.ability_manager.get("skill_complete") is not None:
            return
        card = ToolCard(
            id="skill_complete", name="skill_complete",
            exposure=ToolExposure.DIRECT,
            description="Call when the active skill finishes to release its temporary permissions.",
            input_params={"type": "object", "properties": {"skill_name": {"type": "string"}},
                          "required": ["skill_name"]},
            parallel_safe=False,
        )

        async def complete(skill_name: str) -> dict:
            return {"skill_name": skill_name, "completed": True}

        agent.ability_manager.add_ability(card, LocalFunction(card=card, func=complete))
        self._complete_card = card

    def uninit(self, agent) -> None:
        card = getattr(self, "_complete_card", None)
        if card is not None and agent.ability_manager.get("skill_complete") is card:
            agent.ability_manager.remove_ability("skill_complete")

    def _invoke_session_id(self, ctx):
        return getattr(self, "_session_id", None) or super()._invoke_session_id(ctx)

    def _resolve_scope(self, ctx):
        sid = getattr(self, "_session_id", None)
        return (sid, self._agent_scope_id) if sid else super()._resolve_scope(ctx)

    def resolve_scope(self, ctx: AgentCallbackContext) -> tuple[str, str] | None:
        """Reuse the gate's scope resolution without changing authorization rules."""
        return self._resolve_scope(ctx)

    @property
    def session_id(self) -> str | None:
        return getattr(self, "_session_id", None)

    @property
    def sessions_root(self):
        return getattr(self, "_sessions_root", None)

    def bind_session(self, session_id: str, sessions_root) -> None:
        self._session_id = session_id.strip() or None
        self.set_sessions_root(sessions_root)

    def feature_enabled(self) -> bool:
        return self._feature_enabled()

    def uses_permission_engine(self, engine) -> bool:
        return self._engine is engine

    def build_child_rails(self, scope: str, workspace, registry):
        from openjiuwen.harness.security.core import PermissionEngine

        from .subagent_permission_rail import SubagentPermissionRail, build_tool_permission_approval_sender
        from .subagent_skill_authorization_rail import (
            SubagentSkillAuthorizationRail, build_context_approval_sender, build_context_expiry_sender,
        )

        def approval_session():
            return getattr(self, "_approval_stream_session", None)

        engine = PermissionEngine(config=self._config_provider(), workspace_root=workspace)
        expiry = build_context_expiry_sender(approval_session)
        child_gate = SubagentSkillAuthorizationRail(
            session_id=self.session_id, agent_scope_id=scope, engine=engine,
            grant_store=self.store, skill_resolver=self._skill_resolver, trust_resolver=self._trust_resolver,
            config_provider=self._config_provider, scene_bypass=self._scene_bypass,
            approval_sender=build_context_approval_sender(approval_session),
            approval_expiry_sender=expiry, approval_registry=registry,
        )
        permission = SubagentPermissionRail(
            session_id=self.session_id, agent_scope_id=scope, engine=engine,
            grant_store=self.store, config_provider=self._config_provider,
            approval_sender=build_tool_permission_approval_sender(approval_session),
            approval_expiry_sender=expiry, approval_registry=registry,
        )
        child_gate.set_sessions_root(self.sessions_root)
        permission.set_sessions_root(self.sessions_root)
        return child_gate, permission

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
        self._approval_stream_session = ctx.session
        self._bind_session_files(ctx)
        # The engine may still contain the last tool's overlay. Gate DENY checks
        # must always evaluate the freshly installed baseline, not that overlay.
        if self._engine is not None:
            self._engine.update_config(self._base_effective_config(self._invoke_session_id(ctx)))
        return await super().before_tool_call(ctx)

    async def after_tool_call(self, ctx):
        self._bind_session_files(ctx, sync_policy=False)
        return await super().after_tool_call(ctx)
