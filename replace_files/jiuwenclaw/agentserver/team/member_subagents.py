# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Attach subagent specs to team member ``DeepAgent.deep_config`` before ``SubagentRail`` inits."""

from __future__ import annotations

import logging

from openjiuwen.harness import DeepAgent

logger = logging.getLogger(__name__)


def assign_team_member_subagents(main_deep: DeepAgent, member_agent: DeepAgent) -> None:
    """Copy main DeepAgent's subagents onto member; no-op if main has none."""
    mdc = member_agent.deep_config
    if mdc is None:
        return

    main_dc = getattr(main_deep, "deep_config", None)
    main_sub = getattr(main_dc, "subagents", None) if main_dc else None
    if main_sub:
        mdc.subagents = list(main_sub)
        logger.info("[TeamMemberSubagents] copied %d subagent spec(s) from main DeepAgent", len(main_sub))
    else:
        logger.info("[TeamMemberSubagents] main has no subagents; SubagentRail will skip task_tool")
