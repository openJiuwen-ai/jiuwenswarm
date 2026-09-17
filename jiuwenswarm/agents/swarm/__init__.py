# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm provider-based team assembly package.

Importing this package registers all swarm capability providers and rail types
(idempotent) so a ``TeamAgentSpec`` can be enriched and built without inheriting
from a pre-built single agent. The public surface is the enrichment entry point
``enrich_team_spec_for_swarm`` plus the runtime build context
``SwarmBuildContext``.
"""

from __future__ import annotations

from jiuwenswarm.agents.swarm.assembly import enrich_team_spec_for_swarm
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.registry import register_swarm_providers

register_swarm_providers()

__all__ = [
    "enrich_team_spec_for_swarm",
    "SwarmBuildContext",
    "register_swarm_providers",
]
