# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer leader graph tools — the picked ``build_designer_tools`` surface
mounted as a swarm capability provider.

The provider resolves the session's design context (graph + spawner,
bound by :class:`DesignTeamBridge` before the team turn) and builds the
preserved designer toolset against it: ``designer_graph_get`` /
``designer_graph_patch`` (live against the store via the spawner),
``designer_node_run`` (routes to the coordinator's single-node trigger),
``designer_read_upstream``, and the model/media tools — the picked
package's tool logic and prompts are reused verbatim.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.server.runtime.design.context import (
    StoreNodeSpawner,
    get_design_context_registry,
    get_graph_coordinator,
)
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.node_agent import (
    DesignerGraphToolkit,
    build_designer_tools,
)


@harness_element(
    kind=ElementKind.TOOL,
    name="swarm.designer_graph_tools",
    description="Designer leader graph tools: read/patch the design canvas, "
    "run nodes on demand, call models (picked designer toolset, "
    "session-bound).",
)
def build_designer_graph_tools(
    params: dict[str, Any], ctx: SwarmBuildContext
) -> list[Any]:
    """Mount the designer graph tools for the design team's leader."""
    session_id = str(ctx.session_id or "").strip()
    context = get_design_context_registry().get(session_id)
    if context is None or not context.graph_id:
        raise ValueError(
            "design team session is not bound to a designer graph: "
            f"session_id={session_id!r}"
        )
    store = DesignerGraphStore()
    graph = store.get_graph(context.graph_id)
    if graph is None:
        raise ValueError(
            f"designer graph not found for session: graph_id={context.graph_id!r}"
        )
    if context.spawner is None:
        context.spawner = StoreNodeSpawner(
            store,
            context.graph_id,
            lambda: get_graph_coordinator(store, context.graph_id),
        )
    tool_ctx = NodeExecutionContext(
        graph=graph,
        run_id="",
        node_id="leader",
        run=None,
        emit_activity=None,
        on_prompt_artifact=None,
    )
    toolkit = DesignerGraphToolkit(context.spawner, tool_ctx)
    # Graph authoring (designer_graph_autorun) is NOT mounted here: it is the
    # DesignerAuthoringRail's single capability (two-rail architecture —
    # authoring and execution each own one tool + prompt section; this
    # provider serves graph editing/context reads only).
    return build_designer_tools(toolkit)


__all__ = ["build_designer_graph_tools"]
