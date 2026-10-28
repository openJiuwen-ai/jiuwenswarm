# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design-mode rail providers (team.design profile only).

``swarm.designer_authoring`` mounts the leader's graph-authoring tool
(``designer_graph_autorun``) with its prompt section — a fresh creative
request triggers the service-side Director chain (analyze → brief → review →
storyboard → review → execution graph → validate) instead of the leader
hand-assembling the graph.

``swarm.designer_execution`` mounts the leader's canvas-execution tool
(``swarmflow``) together with its system prompt section — the
"rules for the mapping skeleton, LLM fills the parameters" surface the
design leader uses to trigger a run after the user agrees.

``swarm.designer_surface_sanitizer`` subtracts the capability surfaces the
standard agent-core configurator mounts unconditionally but the design
leader has no use for (workspace_meta, org_* tools, group chat) — the
single-repo alternative to patching agent-core.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ElementKind,
    harness_element,
)

from jiuwenswarm.agents.harness.design.rails.designer_authoring_rail import (
    DesignerAuthoringRail,
)
from jiuwenswarm.agents.harness.design.rails.designer_execution_rail import (
    DesignerExecutionRail,
)
from jiuwenswarm.agents.harness.design.rails.designer_surface_sanitizer_rail import (
    DesignerSurfaceSanitizerRail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

DESIGNER_AUTHORING = "swarm.designer_authoring"
DESIGNER_EXECUTION = "swarm.designer_execution"
DESIGNER_SURFACE_SANITIZER = "swarm.designer_surface_sanitizer"


@harness_element(
    kind=ElementKind.RAIL,
    name=DESIGNER_AUTHORING,
    description="team.design leader authoring tool (designer_graph_autorun) "
    "plus its usage prompt section; a fresh creative request runs the "
    "service-side Director chain that composes the execution graph.",
)
def _build_designer_authoring_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> DesignerAuthoringRail | None:
    _ = params
    session_id = str(getattr(context, "session_id", "") or "").strip()
    if not session_id:
        return None
    return DesignerAuthoringRail(session_id=session_id)


@harness_element(
    kind=ElementKind.RAIL,
    name=DESIGNER_EXECUTION,
    description="team.design leader execution tool (swarmflow) "
    "plus its usage prompt section; canvas trigger from the conversation.",
)
def _build_designer_execution_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> DesignerExecutionRail | None:
    _ = params
    session_id = str(getattr(context, "session_id", "") or "").strip()
    if not session_id:
        return None
    return DesignerExecutionRail(session_id=session_id)


@harness_element(
    kind=ElementKind.RAIL,
    name=DESIGNER_SURFACE_SANITIZER,
    description="Subtract non-design tools/prompt sections from the team.design "
    "leader surface (workspace_meta, org_* tools, group chat, build_team "
    "bootstrap section); single-repo counterpart to agent-core's unconditional "
    "capability mounting.",
)
def _build_designer_surface_sanitizer_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> DesignerSurfaceSanitizerRail:
    _ = params, context
    return DesignerSurfaceSanitizerRail()


__all__ = ["DESIGNER_AUTHORING", "DESIGNER_EXECUTION", "DESIGNER_SURFACE_SANITIZER"]
