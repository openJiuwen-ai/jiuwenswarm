# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design-only authoring rail: the leader's ``designer_graph_autorun`` tool.

The mirror of :class:`DesignerExecutionRail` for graph AUTHORING: mounts
exactly one capability — running the Director chain (creative request →
execution graph, service-side LLM calls) from the conversation — together
with the system prompt section that teaches when to use it. Mounted only on
the team.design leader profile.

Architecture (user review 2026-10-12): the leader has NO hard-coded tools.
Its conversational surface is exactly two design rails plus the canvas tool
provider:

* ``DesignerAuthoringRail``    — designer_graph_autorun (this rail): a fresh
  creative request → the old Director chain runs server-side and saves the
  composed graph; the leader then reports and asks the user to run;
* ``DesignerExecutionRail``    — swarmflow: trigger the compiled
  canvas (full / single node / cascade);
* ``designer_graph_tools``     — designer_graph_get / graph_patch and the
  picked node toolset: small user-driven edits and context reads.

The rail's tool wraps ``director_chain.run_director_chain``; the chain's LLM
calls are independent service-side ``call_model_tool`` calls (not the
leader's), so the leader's own model turns stay conversational.

The tool reproduces the original bootstrap entry (52469f4c
``_bootstrap_graph_with_director``): the ``require_llm`` gate, the whole
chain under ``use_preferred_designer_model`` (the session's UI-selected
model), and — because the chat surface is async (accept-and-trigger) — the
tool ITSELF pushes the canvas events: per-stage
``designer.leader.activity`` frames while the chain runs (the old
``on_progress`` contract) and one ``designer.graph.updated`` frame the
moment the composed graph is saved.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

TOOL_NAME = "designer_graph_autorun"
SECTION_NAME = "designer_authoring"
AUTORUN_FAILURE_HINT = (
    "建图失败：不要用 designer_graph_patch 手动拼图，"
    "把上面的错误原因转告用户，等用户调整需求或要求重试。"
)

_PROMPT_CN = """# 画布建图（designer_graph_autorun）

用户提出**全新的创作需求**（如"帮我做一个关于X的视频/广告/故事"）时调用：

- `prompt`：用户的完整创作需求原文（或整理后的完整需求）——它是导演链的创意输入。用户在需求里写的画幅/分辨率/时长会被识别并写入画布设置。

服务端会按导演链建图（分析→写创意简报→审核→写分镜→审核→生成执行图→校验），产出带完整锁体系的图并推送到画布。调用约定：

- 建图成功后**直接调用 `swarmflow` 开始执行**（不带参数），不需要先询问用户；调用后告诉用户"图已生成，执行已开始，画布可看进度"。
- 工具返回后，简明汇报图的结构（几个角色/场景/镜头）。
- 用户明确说"先不要跑/先看看图"等拒绝执行的意思时，不要调用 run_graph，等用户确认。
- 不要自己用 graph_patch 从零拼整图——graph_patch 只用于用户要求的小改（加减节点/改提示词/连线/删节点）。
- 建图耗时较长（多次服务端模型调用）；执行期间可告诉用户正在生成。
"""

_PROMPT_EN = """# Canvas authoring (designer_graph_autorun)

Call this when the user brings a FRESH creative request ("make a video about
X"). Pass the user's complete request as ``prompt``. The Director chain runs server-side (analyze → brief →
review → storyboard → review → execution graph → validate), saves the
composed graph to the canvas, and the tool returns a structure summary.

On success, IMMEDIATELY call ``swarmflow`` (no args) to start the
run — do not ask first; tell the user the graph is built, execution has
started and progress is on the canvas. Only hold off when the user has
explicitly said not to run yet. Report the structure briefly (roles/scenes/
clips). Do NOT hand-assemble whole graphs with graph_patch; patch is for
small user-driven edits only.
"""


def _retitle_session(session_id: str, graph: dict[str, Any], prompt: str) -> None:
    """Name the session after the model's short story title, never the prompt."""
    from jiuwenswarm.common.schema.designer_node_labels import usable_story_title
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    title = usable_story_title(analysis.get("story_name"), prompt)
    if not title:
        return
    try:
        update_session_metadata(
            session_id=session_id,
            title=title,
            touch_last_message_at=False,
            sync_write=True,
        )
    except Exception:  # noqa: BLE001 — a title is cosmetic
        logger.debug("[DesignerAuthoringRail] session retitle failed", exc_info=True)


class DesignerAuthoringRail(DeepAgentRail):
    """Leader-only authoring tool + its prompt section (team.design)."""

    priority = 60

    def __init__(self, *, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id
        self._graph_id: str = ""
        self.system_prompt_builder = None
        self._tool: LocalFunction | None = None

    # ------------------------------------------------------------------
    # tool
    # ------------------------------------------------------------------

    def _build_tool(self) -> LocalFunction:
        from jiuwenswarm.server.runtime.design.context import (
            get_design_context_registry,
        )
        from jiuwenswarm.server.runtime.design.director_chain import (
            DirectorChainError,
            run_director_chain,
        )
        from jiuwenswarm.server.runtime.design.team_bridge import (
            push_graph_update,
            push_leader_activity,
        )
        from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

        def _stage_emitter(channel_id: str, session_id: str):
            # The chain's ``_stage`` helper calls back synchronously; schedule
            # the async gateway push on the running loop so each step reaches
            # the canvas peek live (the original bootstrap's on_progress).
            import asyncio as _asyncio

            def emit(kind: str, text: str, tool: str = "") -> None:
                try:
                    loop = _asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(
                    push_leader_activity(channel_id, session_id, kind, text, tool)
                )

            return emit

        async def _autorun(**kwargs: Any) -> str:
            prompt = str(kwargs.get("prompt") or "").strip()
            store = DesignerGraphStore()
            graph = store.get_graph(self._graph_id)
            if graph is None:
                return "error: designer graph not found"
            context = get_design_context_registry().get(self._session_id)
            if context is not None and context.spawner is not None and context.spawner.report_pending:
                from jiuwenswarm.agents.harness.design.rails.designer_execution_rail import (
                    REPORT_ONLY_REFUSAL,
                )

                return REPORT_ONLY_REFUSAL
            channel_id = (
                context.channel_id if context is not None and context.channel_id else "web"
            )
            from jiuwenswarm.server.runtime.designer.model_tools import (
                DesignerLlmError,
                require_llm,
                use_preferred_designer_model,
            )

            # Original bootstrap entry contract (52469f4c
            # ``_bootstrap_graph_with_director``): credential gate up front,
            # and the whole chain runs on the session's UI-selected model.
            try:
                require_llm()
            except DesignerLlmError as exc:
                return f"error: {exc.user_message}\n{AUTORUN_FAILURE_HINT}"
            preferred_model = str(
                (graph.get("metadata") or {}).get("model_name") or ""
            ).strip()
            try:
                with use_preferred_designer_model(preferred_model):
                    composed = await run_director_chain(
                        graph,
                        prompt=prompt,
                        on_stage=_stage_emitter(channel_id, self._session_id),
                    )
            except DirectorChainError as exc:
                return f"error: {exc.user_message}\n{AUTORUN_FAILURE_HINT}"
            saved = store.save_graph(composed)
            _retitle_session(self._session_id, saved, prompt)
            # The chat surface is async (accept-and-trigger): nothing else
            # tells the canvas the composed graph exists — the tool pushes it.
            push_graph_update(channel_id, self._session_id, dict(saved))
            nodes = saved.get("nodes") or []
            roles: dict[str, int] = {}
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
                key = str(cfg.get("pipeline") or node.get("type") or "?")
                roles[key] = roles.get(key, 0) + 1
            summary = ", ".join(f"{v}×{k}" for k, v in sorted(roles.items()))
            return (
                f"建图完成：{len(nodes)} 个节点（{summary}）。"
                "图已保存并推送到画布；请直接调用 swarmflow 开始执行。"
            )

        card = ToolCard(
            name=TOOL_NAME,
            description=(
                "按导演链自动建图（分析→写创意简报→审核→写分镜→审核→生成执行图→校验）。"
                "用户提出全新的创作需求时用这个工具，不要自己手拼 graph_patch 建整图；"
                "graph_patch 只用于用户要求的小改。参数：prompt=用户的完整创作需求原文。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "用户的完整创作需求（原话或整理后的需求），将作为建图的创意输入",
                    },
                },
                "required": ["prompt"],
            },
            # The chain is SIX serial service-side LLM calls (analyze→brief→
            # review→storyboard→review→graph→validate); each step owns its
            # internal timeout (call_model_tool timeout_sec + retries). The
            # generic 300s ability-manager deadline kills the chain mid-step
            # (first real run: author_creative_brief's 65536-max_tokens call
            # alone outlived it), so exempt this tool from the outer deadline
            # — internal timeouts govern (ability_manager resilience contract:
            # timeout_s=None → exempt).
            properties={
                "resilience": {"timeout_s": None},
            },
        )
        return LocalFunction(card=card, func=_autorun)

    # ------------------------------------------------------------------
    # rail lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: Any) -> None:
        from jiuwenswarm.server.runtime.design.context import (
            get_design_context_registry,
        )

        context = get_design_context_registry().get(self._session_id)
        if context is None or not context.graph_id:
            logger.warning(
                "[DesignerAuthoringRail] session not bound to a designer "
                "graph; tool not mounted: session_id=%s",
                self._session_id,
            )
            return
        self._graph_id = str(context.graph_id)
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._tool = self._build_tool()
        agent.ability_manager.add_ability(self._tool.card, self._tool)
        logger.info(
            "[DesignerAuthoringRail] mounted %s for session %s",
            TOOL_NAME,
            self._session_id,
        )

    def uninit(self, agent: Any) -> None:
        try:
            agent.ability_manager.remove(TOOL_NAME)
        except Exception:  # noqa: BLE001
            logger.debug("[DesignerAuthoringRail] tool already absent on uninit")
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(SECTION_NAME)
        self._tool = None
        self._graph_id = ""
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: Any) -> None:
        _ = ctx
        if self.system_prompt_builder is None or self._tool is None:
            return
        self.system_prompt_builder.add_section(
            PromptSection(
                name=SECTION_NAME,
                content={"cn": _PROMPT_CN, "en": _PROMPT_EN},
                priority=60,
            )
        )


__all__ = ["DesignerAuthoringRail", "TOOL_NAME"]
