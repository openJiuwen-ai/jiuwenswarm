# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent Heartbeat Rail shared by single-agent and Team profiles.

The process-level :class:`HeartbeatRailRuntime` owns scheduling and execution.
Each work/code single-agent or Team member mounts this rail and receives the
same runtime through dependency injection, so tools and wake-up execution share
one authoritative service without creating one scheduler per agent.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import (
    HEARTBEAT_TOOL_NAMES,
    HeartbeatJobService,
    HeartbeatRuntimeBridge,
)

logger = logging.getLogger(__name__)


class HeartbeatRail(DeepAgentRail):
    """Expose AgentServer-local Heartbeat tools to single and Team agents."""

    priority = 80

    def __init__(self, *, service: HeartbeatJobService, context: Any) -> None:
        super().__init__()
        self._runtime = HeartbeatRuntimeBridge(service)
        self._context = context
        self._tools: list[Any] = []

    def init(self, agent: Any) -> None:
        """Register rail-owned tools on the concrete DeepAgent instance."""
        self._tools = self._runtime.build_tools(context=self._context)
        for tool in self._tools:
            agent.ability_manager.add_ability(tool.card, tool)
        logger.info(
            "[HeartbeatRail] registered tools: %s",
            sorted(getattr(tool.card, "name", "") for tool in self._tools),
        )

    def uninit(self, agent: Any) -> None:
        """Remove only tools owned by this rail."""
        for name in HEARTBEAT_TOOL_NAMES:
            try:
                agent.ability_manager.remove(name)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[HeartbeatRail] tool already absent during uninit: %s", name
                )
        self._tools = []


__all__ = ["HeartbeatRail"]
