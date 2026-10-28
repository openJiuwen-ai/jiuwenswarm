# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design-only execution rail: the leader's ``swarmflow`` tool.

Mounts exactly one capability — triggering the canvas execution (graph →
swarmflow compilation → run) from the conversation — together with the system
prompt section that teaches when and how to use it. Mounted only on the
team.design leader profile; every other mode never sees it.

The tool wraps :meth:`StoreNodeSpawner.run_graph` (trigger re-entry guard +
per-graph coordinator) and mirrors the canvas run button's three modes:
no args = run every unproduced node, ``node_id`` = single-node rerun
(overwrites the current output; the previous version is archived into
history), ``node_id`` + ``cascade`` = rerun from that node
through its downstream closure.

Async shape (2026-10-13, mirroring the ``swarmflow()`` tool): a full graph
run is many worker LLM turns plus media generation — the tool call must not
hold the leader's round hostage. ``invoke`` launches the trigger on the
harness's ``async_tool_runtime`` and returns a "launched" message at once;
progress streams to the canvas meanwhile, and the run summary is injected
back as a follow-up message when the trigger finishes. That follow-up round
is report-only: whatever the run status, the leader states the outcome and
waits for the user. ``report_pending`` on the spawner enforces it — run and
rebuild tools refuse until the next user chat round clears it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)


TOOL_NAME = "swarmflow"
SECTION_NAME = "designer_execution"
# Per-node text preview cap in the tool result: enough for the leader to
# report outcomes, never a full document dump.
_NODE_TEXT_PREVIEW_CHARS = 240
_REPORT_ONLY_NOTE = (
    "画布运行已结束。本轮只如实向用户汇报下列结果（无论成功或失败），"
    "不要重新运行、不要改图，等待用户下一条消息。"
)
REPORT_ONLY_REFUSAL = (
    "已拒绝：刚结束的运行还在汇报阶段。如实向用户汇报运行结果即可，"
    "是否重跑由用户下一条消息决定。"
)

_PROMPT_CN = """# 画布执行（swarmflow）

征得用户同意后，你可以直接触发设计画布的执行（与画布上的运行按钮完全等价）：

- 不带参数：运行所有还没有产出的节点（首次全量运行或补跑新加的节点）。
- 只给 `node_id`：只重跑该节点。新产出直接覆盖当前产出，旧版本归档进 history；下游节点绝不会跟着重跑。
- `node_id` + `cascade=true`：从该节点起连同它的所有下游一起重跑（结果直接覆盖）；上游节点保持现有产出不变，即使它的产出基于更早的输入也原样使用。

调用约定：
- 工具调用**立即返回**（执行在后台进行），进度实时推送到画布；执行结束后你会收到一条运行摘要消息，届时用一两句话如实向用户汇报各节点的结果与状态，不要粘贴全文。
- 收到运行摘要的这一轮**只汇报**：不论成功、部分失败还是全部失败，跑完了就是跑完了。不要再次调用 swarmflow / designer_graph_autorun，也不要为了"修复"而改图；等用户下一条消息再决定。
- 同一时刻只允许一个执行；执行中再次调用会被拒绝，此时告诉用户稍候。
- 节点失败时摘要里会带 error，向用户说明失败节点与原因，可以给出建议（改提示词重跑该节点 / 从该节点续跑），但不要自行执行。
- 调用后先告诉用户"已开始执行，画布可看进度"，不要在原地等待结果。
"""

_PROMPT_EN = """# Canvas execution (swarmflow)

After the user agrees, you may trigger the canvas execution directly (same
semantics as the canvas run button):

- No args: run every node that has no output yet (first full run, or catch
  up newly added nodes).
- `node_id` only: rerun just that node. The fresh output overwrites the
  node's current output directly and the previous version is archived into
  history; downstream nodes never re-execute.
- `node_id` + `cascade=true`: rerun that node plus its whole downstream
  closure (outputs overwrite directly); upstream nodes keep their current
  outputs as-is.

Conventions:
- The tool call returns IMMEDIATELY (the run executes in the background);
  progress streams to the canvas meanwhile. A run-summary message is
  delivered to you when it finishes — report each node's outcome truthfully
  in a sentence or two then, never paste full texts.
- The round that receives the run summary is REPORT-ONLY: succeeded,
  partially failed or failed, the run is over. Do not call
  swarmflow / designer_graph_autorun again and do not edit the
  graph to "fix" it; wait for the user's next message.
- Only one execution at a time; a second call while one runs is rejected —
  tell the user to wait.
- Node failures carry an error in the summary: name the node, the reason,
  and you may suggest the next step (edit the prompt and rerun / rerun from
  there) — never carry it out yourself.
- After launching, tell the user "execution started, watch the canvas" —
  do not wait in place for the result.
"""


def _run_summary(run: dict[str, Any]) -> str:
    """Compact run digest for the leader's reply (never a full dump)."""
    states = run.get("node_states") or {}
    nodes: dict[str, Any] = {}
    for node_id, state in states.items():
        if not isinstance(state, dict):
            continue
        entry: dict[str, Any] = {"status": state.get("status")}
        text = str(state.get("text") or "")
        if text:
            entry["text"] = text[:_NODE_TEXT_PREVIEW_CHARS]
        error = str(state.get("error") or "")
        if error:
            entry["error"] = error[:200]
        nodes[node_id] = entry
    digest = json.dumps(
        {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "nodes": nodes,
        },
        ensure_ascii=False,
    )
    return f"{_REPORT_ONLY_NOTE}\n{digest}"


class DesignerExecutionRail(DeepAgentRail):
    """Leader-only execution tool + its prompt section (team.design)."""

    priority = 60

    def __init__(self, *, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id
        self._spawner: Any = None
        self._agent: Any = None
        self.system_prompt_builder = None
        self._tool: LocalFunction | None = None

    # ------------------------------------------------------------------
    # tool
    # ------------------------------------------------------------------

    def _build_tool(self) -> LocalFunction:
        spawner = self._spawner

        async def _run_background(task_id: str, inputs: dict[str, Any]) -> str:
            node_id = str(inputs.get("node_id") or "").strip()
            cascade = bool(inputs.get("cascade"))
            node_ids = [node_id] if node_id else None
            try:
                run = await spawner.run_graph(node_ids=node_ids, cascade=cascade)
            except RuntimeError as exc:
                return f"执行被拒绝：{exc}"
            finally:
                spawner.report_pending = True
            return _run_summary(run)

        async def _run_graph(**kwargs: Any) -> str:
            node_id = str(kwargs.get("node_id") or "").strip()
            cascade = bool(kwargs.get("cascade"))
            if cascade and not node_id:
                return "cascade requires node_id"
            if spawner.report_pending:
                return REPORT_ONLY_REFUSAL
            mode = "单点重跑" if node_id and not cascade else (
                "从该节点续跑" if node_id else "全量运行"
            )
            task_id = f"swarmflow_{node_id or 'full'}_{id(_run_graph):x}"
            try:
                # Async-tool contract (mirrors swarmflow()): launch on the
                # harness's runtime, return immediately; the summary is
                # injected back as a follow-up message on completion.
                self._agent.async_tool_runtime.launch(
                    task_id,
                    lambda: _run_background(task_id, dict(kwargs)),
                    tool_name=TOOL_NAME,
                    description=f"画布执行（{mode}）",
                )
            except RuntimeError as exc:
                return f"执行被拒绝：{exc}"
            return (
                f"已开始{mode}：执行在后台进行，进度实时推送到画布。"
                "不要重复调用；执行结束后会收到包含各节点结果的运行摘要，"
                "届时再向用户汇报。"
            )

        card = ToolCard(
            name=TOOL_NAME,
            description=(
                "触发设计画布执行（把当前图编译为工作流并在后台运行）。不带参数="
                "运行所有没有产出的节点；node_id=只重跑该节点（新产出直接覆盖当前"
                "产出，旧版本归档进 history，下游不重跑）；"
                "node_id+cascade=true=从该节点起连同其下游一起"
                "重跑（直接覆盖），上游保持现有产出。调用立即返回，执行在后台进行，"
                "结束后自动收到运行摘要。须先征得用户同意。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "cascade": {"type": "boolean"},
                },
            },
        )
        return LocalFunction(card=card, func=_run_graph)

    # ------------------------------------------------------------------
    # rail lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: Any) -> None:
        from jiuwenswarm.server.runtime.design.context import (
            get_design_context_registry,
        )

        context = get_design_context_registry().get(self._session_id)
        if context is None or context.spawner is None:
            logger.warning(
                "[DesignerExecutionRail] session not bound to a designer "
                "graph; tool not mounted: session_id=%s",
                self._session_id,
            )
            return
        self._spawner = context.spawner
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._tool = self._build_tool()
        agent.ability_manager.add_ability(self._tool.card, self._tool)
        logger.info(
            "[DesignerExecutionRail] mounted %s for session %s",
            TOOL_NAME,
            self._session_id,
        )

    def uninit(self, agent: Any) -> None:
        try:
            agent.ability_manager.remove(TOOL_NAME)
        except Exception:  # noqa: BLE001
            logger.debug("[DesignerExecutionRail] tool already absent on uninit")
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(SECTION_NAME)
        self._tool = None
        self._spawner = None
        self._agent = None
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: Any) -> None:
        _ = ctx
        if self.system_prompt_builder is None or self._spawner is None:
            return
        self.system_prompt_builder.add_section(
            PromptSection(
                name=SECTION_NAME,
                content={"cn": _PROMPT_CN, "en": _PROMPT_EN},
                priority=60,
            )
        )


__all__ = ["DesignerExecutionRail", "REPORT_ONLY_REFUSAL", "TOOL_NAME"]
