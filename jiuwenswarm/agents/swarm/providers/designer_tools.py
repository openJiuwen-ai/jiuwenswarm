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

import json
from typing import Any

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_BRIEF, node_pipeline
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

STORY_EDIT_TOOL_NAME = "designer_story_edit"


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
    return [*build_designer_tools(toolkit), _build_story_edit_tool(context.spawner)]


def _build_story_edit_tool(spawner: StoreNodeSpawner) -> Any:
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

    async def story_edit(**kwargs: Any) -> str:
        from jiuwenswarm.agents.harness.design.rails.designer_execution_rail import (
            REPORT_ONLY_REFUSAL,
        )

        if spawner.report_pending:
            return REPORT_ONLY_REFUSAL
        edit = str(kwargs.get("edit") or "").strip()
        if not edit:
            return "error: edit is required"
        try:
            graph, changed = await spawner.apply_story_edit(edit)
        except (RuntimeError, ValueError) as exc:
            return f"error: {exc}"
        brief_ids = [
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if isinstance(node, dict) and node_pipeline(node) == NODE_ROLE_BRIEF
        ]
        return json.dumps(
            {
                "revised": changed,
                "brief_node_id": brief_ids[0] if brief_ids else None,
                "next": (
                    "The approved brief, cast and storyboard are saved and the clip beats "
                    "were re-synced. Patch any character / scene / clip prompt that still "
                    "states the old fact, then rerun from the brief node with "
                    "swarmflow(node_id=brief_node_id, cascade=true) so the brief, "
                    "storyboard and every downstream output pick up the edit."
                ),
            },
            ensure_ascii=False,
        )

    card = ToolCard(
        name=STORY_EDIT_TOOL_NAME,
        description=(
            "修改故事层面的事实（人物的性别/身份/外貌/服装、地点、时间、情节、镜头里的道具或场景细节等）时使用："
            "按修改要求同步修订导演已批准的大纲（Brief）、角色设定和分镜表，并保存到画布。"
            "只 patch 下游节点的提示词改不到大纲和分镜。参数：edit=用户的修改要求原话。"
        ),
        input_params={
            "type": "object",
            "properties": {
                "edit": {
                    "type": "string",
                    "description": "用户的修改要求（原话或整理后的完整要求），如“把全片的女店员改成男店员”",
                },
            },
            "required": ["edit"],
        },
        # Up to three serial service-side LLM calls (brief, cast, storyboard);
        # each call's own timeout governs, not the generic tool deadline.
        properties={"resilience": {"timeout_s": None}},
    )
    return LocalFunction(card=card, func=story_edit)


__all__ = ["STORY_EDIT_TOOL_NAME", "build_designer_graph_tools"]
