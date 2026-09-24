# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-invoke mounting of request-scoped MCP tools on Team members."""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.mcp_config import (
    OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX,
    clear_agent_office_claw_tool_ids,
    get_request_scoped_mcp_registration,
    set_agent_office_claw_tool_ids,
)

logger = logging.getLogger(__name__)

_MOUNT_STATE_KEY = "jiuwenswarm.request_scoped_mcp_tools"


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

        registration = get_request_scoped_mcp_registration(self._session_id)
        if registration is None:
            clear_agent_office_claw_tool_ids(agent)
            return

        allowed_ids = frozenset(registration.tool_ids)
        mounted: list[tuple[str, str]] = []
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
                if not existing_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX):
                    logger.warning(
                        "[RequestScopedMcpToolsRail] request tool conflicts with "
                        "member ability; keeping existing: session_id=%s "
                        "request_id=%s name=%s existing_id=%s new_id=%s",
                        self._session_id,
                        registration.request_id,
                        name,
                        existing_id,
                        tool_id,
                    )
                    continue
                ability_manager.remove(name)

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

        mounted_ids = frozenset(tool_id for _, tool_id in mounted)
        if mounted_ids:
            set_agent_office_claw_tool_ids(agent, mounted_ids)
        else:
            clear_agent_office_claw_tool_ids(agent)
        ctx.extra[_MOUNT_STATE_KEY] = (mounted, mounted_ids)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        state = ctx.extra.pop(_MOUNT_STATE_KEY, None)
        if state is None:
            return
        mounted, mounted_ids = state
        agent = getattr(ctx, "agent", None)
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return

        for name, tool_id in mounted:
            existing = ability_manager.get(name)
            if str(getattr(existing, "id", "") or "") == tool_id:
                ability_manager.remove(name)
        clear_agent_office_claw_tool_ids(agent, expected_tool_ids=mounted_ids)


__all__ = ["RequestScopedMcpToolsRail"]
