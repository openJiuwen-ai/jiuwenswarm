# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-invoke mounting of request-scoped MCP tools on Team members."""

from __future__ import annotations

import logging
from functools import wraps

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.mcp_config import (
    _active_office_claw_tool_ids,
    bind_active_office_claw_mcp_tools,
    clear_agent_office_claw_tool_ids,
    get_request_scoped_mcp_registration,
    set_agent_office_claw_tool_ids,
)

logger = logging.getLogger(__name__)

_MOUNT_STATE_KEY = "jiuwenswarm.request_scoped_mcp_tools"
_MOUNT_STATE_ATTR = "_swarm_request_scoped_mcp_mount"
_BINDING_TOKEN_ATTR = "_swarm_request_scoped_mcp_token"


class RequestScopedMcpToolsRail(DeepAgentRail):
    """Mount the current request's MCP cards for one Team member invocation."""

    priority = 99
    inherit_to_subagents = False

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = str(session_id or "").strip()

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        agent = getattr(ctx, "agent", None)
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return

        # A resumed member may never have reached the preceding after_invoke.
        # Remove only cards this rail installed, including tools absent from the
        # new generation. An old callback must not clean up this new mount.
        previous = getattr(ability_manager, _MOUNT_STATE_ATTR, None)
        if previous is not None:
            self._unmount(agent, ability_manager, previous)
        registration = get_request_scoped_mcp_registration(self._session_id)
        if registration is None:
            return

        allowed_ids = frozenset(registration.tool_ids)
        mounted: list[tuple[str, str]] = []
        visible_ids: set[str] = set()
        for tool in registration.tool_instances:
            card = getattr(tool, "card", None)
            name = str(getattr(card, "name", "") or "").strip()
            tool_id = str(getattr(card, "id", "") or "").strip()
            if not name or tool_id not in allowed_ids:
                logger.warning(
                    "[RequestScopedMcpToolsRail] skip invalid request tool card: "
                    "session_id=%s request_id=%s name=%s tool_id=%s",
                    self._session_id,
                    registration.request_id,
                    name,
                    tool_id,
                )
                continue

            existing = ability_manager.get(name)
            existing_id = str(getattr(existing, "id", "") or "")
            if existing is not None and existing_id != tool_id:
                logger.warning(
                    "[RequestScopedMcpToolsRail] request tool conflicts with "
                    "member ability; keeping existing: session_id=%s "
                    "request_id=%s name=%s existing_id=%s new_id=%s",
                    self._session_id, registration.request_id, name, existing_id, tool_id,
                )
                continue
            if existing is not None:
                # This card belongs to another installer; allow it but do not
                # claim ownership or remove it in after_invoke.
                visible_ids.add(tool_id)
                continue

            result = ability_manager.add(card)
            added = getattr(result, "added", None) if result is not None else None
            installed = ability_manager.get(name)
            if added is False or str(getattr(installed, "id", "") or "") != tool_id:
                logger.warning(
                    "[RequestScopedMcpToolsRail] failed to mount request tool: "
                    "session_id=%s request_id=%s name=%s tool_id=%s",
                    self._session_id,
                    registration.request_id,
                    name,
                    tool_id,
                )
                continue
            mounted.append((name, tool_id))
            visible_ids.add(tool_id)

        mounted_ids = frozenset(visible_ids)
        if mounted_ids:
            set_agent_office_claw_tool_ids(agent, mounted_ids)
        else:
            clear_agent_office_claw_tool_ids(agent)
        original_execute = getattr(ability_manager, "execute", None)
        bound_execute = None
        if original_execute is not None:
            @wraps(original_execute)
            async def bound_execute(*args, **kwargs):
                # Core can swallow cancellation of non-parallel tools while
                # skipping after hooks. Restore the caller's context regardless.
                with bind_active_office_claw_mcp_tools(self._allowed_tool_ids(ability_manager)):
                    return await original_execute(*args, **kwargs)

            ability_manager.execute = bound_execute
        state = (mounted, mounted_ids, original_execute, bound_execute)
        setattr(ability_manager, _MOUNT_STATE_ATTR, state)
        ctx.extra[_MOUNT_STATE_KEY] = state

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        state = ctx.extra.pop(_MOUNT_STATE_KEY, None)
        if state is None:
            return
        agent = getattr(ctx, "agent", None)
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return

        if getattr(ability_manager, _MOUNT_STATE_ATTR, None) is state:
            self._unmount(agent, ability_manager, state)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        # Tool tasks can inherit an old (even empty) scheduler ContextVar.
        # Only authorize tools mounted on this member AND still registered.
        manager = getattr(ctx.agent, "ability_manager", None)
        token = _active_office_claw_tool_ids.set(self._allowed_tool_ids(manager))
        # Tool contexts share ctx.extra, so keep the token on the individual
        # context. Use a raw token so cancellation cannot finalize a CM in a
        # different task; the execute wrapper also restores sequential callers.
        setattr(ctx, _BINDING_TOKEN_ATTR, token)

    def _allowed_tool_ids(self, manager) -> frozenset[str]:
        state = getattr(manager, _MOUNT_STATE_ATTR, None)
        registration = get_request_scoped_mcp_registration(self._session_id)
        return state[1].intersection(registration.tool_ids) if state and registration else frozenset()

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._restore_tool_binding(ctx)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        # Retries skip after_tool_call; restore before the next attempt binds.
        self._restore_tool_binding(ctx)

    @staticmethod
    def _restore_tool_binding(ctx: AgentCallbackContext) -> None:
        token = getattr(ctx, _BINDING_TOKEN_ATTR, None)
        if token is not None:
            delattr(ctx, _BINDING_TOKEN_ATTR)
            _active_office_claw_tool_ids.reset(token)

    @staticmethod
    def _unmount(agent, ability_manager, state) -> None:
        mounted, mounted_ids, original_execute, bound_execute = state
        for name, tool_id in mounted:
            existing = ability_manager.get(name)
            if str(getattr(existing, "id", "") or "") == tool_id:
                ability_manager.remove(name)
        clear_agent_office_claw_tool_ids(agent, expected_tool_ids=mounted_ids)
        if bound_execute is not None and getattr(ability_manager, "execute", None) is bound_execute:
            ability_manager.execute = original_execute
        delattr(ability_manager, _MOUNT_STATE_ATTR)


__all__ = ["RequestScopedMcpToolsRail"]
