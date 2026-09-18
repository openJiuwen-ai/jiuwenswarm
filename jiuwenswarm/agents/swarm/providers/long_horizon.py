# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm provider for the ordinary-Agent ``long_horizon_task`` tool.

Registered in the manifest catalog for discovery, but intentionally **not**
mounted on Team / Code member capability specs. Ordinary Agent mode wires the
tool through ``interface_deep`` and ProgressiveToolRail eager tools.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    harness_element,
)

from jiuwenswarm.agents.harness.common.long_horizon.tools import get_decorated_tools
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.tool_ownership import mark_stateless

logger = logging.getLogger(__name__)

LONG_HORIZON = "swarm.long_horizon"


@harness_element(
    kind=ElementKind.TOOL,
    name=LONG_HORIZON,
    description=(
        "Long-horizon multi-stage task tool (long_horizon_task). "
        "Ordinary Agent only; not mounted on Team/Code member specs."
    ),
    input_model=ConstructionInput,
)
def build_long_horizon_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the ``long_horizon_task`` tool instance."""
    del params
    language = getattr(ctx, "language", None) or "cn"
    try:
        return mark_stateless(list(get_decorated_tools(language=language)))
    except Exception as exc:
        logger.warning("[swarm.long_horizon] construction failed: %s", exc)
        return []


__all__ = [
    "LONG_HORIZON",
    "build_long_horizon_tools",
]
