# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillProtocolPromptRail — 注入「技能执行规范」提示词。

技能清单与 ``skill_tool`` / ``skill_complete`` 约定由上游 SkillUseRail 的 ``skills`` 段负责；
本 rail 的 ``skill_protocol`` 与之对齐（加载 SKILL.md 正文**只能**使用 ``skill_tool``：
只有它会走 agent 侧 ``active_skill_bodies`` / 消息保护与 pin 注入；
**禁止**用其它工具冒充等价加载），
并随生命周期注册统一的 ``skill_step`` facade 工具。
"""

from __future__ import annotations

import contextvars
from typing import Any, Optional

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.deep_agent.prompt_builder import PromptPriority
from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
    SkillPhase,
    get_session_active_skill,
    get_session_phase,
)
from jiuwenclaw.utils import logger


_SKILL_PROTOCOL_SECTION_NAME = "skill_protocol"
_SKILL_PLAN_REQUIRED_SECTION_NAME = "skill_plan_required"
_SKILL_COMPLETE_REQUIRED_SECTION_NAME = "skill_complete_required"
# Slot just above SKILL_PROTOCOL so phase-driven directives render adjacent
# to the protocol section without disturbing other priorities.
_SKILL_PHASE_DIRECTIVE_PRIORITY = int(PromptPriority.SKILL_PROTOCOL) - 1

_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "skill_prompt_rail_session_id", default=None,
)


def _extract_conversation_id(ctx: AgentCallbackContext) -> Optional[str]:
    inputs = getattr(ctx, "inputs", None)
    if inputs is None:
        return None
    conv_id = getattr(inputs, "conversation_id", None)
    return str(conv_id) if conv_id else None


def _build_skill_plan_required_text(language: str, skill_name: str) -> str:
    if language == "cn":
        return (
            f"## 强制规约：必须先创建 skill_step 计划（技能 {skill_name}）\n\n"
            f"当前 session 已加载 SKILL.md（技能：{skill_name}），但**尚未**创建对应的 "
            f"skill_step 计划。在创建计划之前：\n\n"
            f"1. **禁止**执行 SKILL.md 中的任何步骤、脚本、工具调用（包括"
            f"『先看一下』『先调用一次试试』等）。\n"
            f"2. **必须**首先调用 `skill_step(action=\"create\")`，为 SKILL.md 中定义的每个步骤"
            f"创建一个 skill_step 项，作为执行路线图。\n"
            f"3. 只有 skill_step 计划创建完成后，才能开始按顺序执行第一个步骤。\n\n"
            f"⚠️ 这是『中途进入』也必须遵守的硬性约束——无论你是从对话开头还是中途加载的 "
            f"SKILL.md，都必须先 create 再执行。\n"
        )
    return (
        f"## Mandatory: Create skill_step plan first (skill: {skill_name})\n\n"
        f"This session has loaded SKILL.md (skill: {skill_name}) but has NOT yet "
        f"created the corresponding skill_step plan. Before the plan is created:\n\n"
        f"1. You MUST NOT execute any step, script, or tool call from SKILL.md "
        f"(including 'just take a look' or 'try calling it once').\n"
        f"2. You MUST first call `skill_step(action=\"create\")` with one skill_step item per "
        f"step defined in SKILL.md as the execution roadmap.\n"
        f"3. Only after the skill_step plan is created may you begin executing the "
        f"first step in order.\n\n"
        f"This is a hard constraint that applies even when SKILL.md is loaded "
        f"mid-session — regardless of whether you loaded it at the start of the "
        f"conversation or partway through, you MUST create the plan first.\n"
    )


def _build_skill_complete_required_text(language: str, skill_name: str) -> str:
    if language == "cn":
        return (
            f"## 强制规约：必须调用 skill_complete 收尾（技能 {skill_name}）\n\n"
            f"当前 session 的所有 skill_step 项已经全部完成（技能：{skill_name}）。"
            f"在调用 `skill_complete(skill_name=\"{skill_name}\")` 收尾之前：\n\n"
            f"1. **禁止**继续执行 SKILL.md 中的任何步骤——它们都已完成。\n"
            f"2. **禁止**为同一技能再次 `skill_step(action=\"create\")` "
            f"或 `skill_step(action=\"insert\")` 添加新任务。\n"
            f"3. **必须**立即调用 `skill_complete(skill_name=\"{skill_name}\")` 释放技能上下文。\n\n"
            f"只有调用 skill_complete 之后，才能进入下一个任务（或下一个技能）。\n"
        )
    return (
        f"## Mandatory: Call skill_complete to finalize (skill: {skill_name})\n\n"
        f"All skill_step items for this session have been completed (skill: {skill_name}). "
        f"Before calling `skill_complete(skill_name=\"{skill_name}\")` to finalize:\n\n"
        f"1. You MUST NOT execute additional steps from SKILL.md — they are all done.\n"
        f"2. You MUST NOT add new tasks via `skill_step(action=\"create\")` "
        f"or `skill_step(action=\"insert\")` "
        f"for this same skill.\n"
        f"3. You MUST call `skill_complete(skill_name=\"{skill_name}\")` immediately to "
        f"release the skill context.\n\n"
        f"Only after skill_complete may you proceed to the next task (or next skill).\n"
    )


def _build_skill_protocol_section_text(language: str) -> str:
    if language == "cn":
        return """## 技能执行规范（强制）

可用技能清单与加载/释放约定见本 prompt 的「技能」段（SkillUseRail 注入），**须与该段一致**。

**加载 SKILL.md 正文（禁止用 bash 执行工具名）：**
- **必须**且**只能**使用 `skill_tool(skill_name=..., relative_file_path="SKILL.md")` 加载；整段技能执行结束且不再需要正文时调用 `skill_complete(skill_name=...)`。不要把这些名字当作 shell 命令。
- 只有 `skill_tool` 会走系统集成路径（正文入会话、后续上下文中的保护与 [ACTIVE SKILL BODY] 注入），**禁止**用任何其它工具加载或拼凑 SKILL.md。
- 若你当前可用工具列表中**没有** `skill_tool`：无法按本系统路径加载技能正文，请向用户说明环境未开放该能力；**不得**用其它工具代替。
- 需要多看或刷新全文时，**只能**再次调用 `skill_tool`。

随后按 SKILL 工作流执行；下列规范约束执行过程。

1. **声明步骤**：每次行动前，必须在回复开头声明当前所在步骤，格式：`[当前步骤: <步骤名称>]`。**无需调用任何工具来"开始"步骤**——声明本身即代表进入该步。
2. **创建路线图**：读完 SKILL.md 后，调用一次 `skill_step(action="create", tasks=[...])`，按 SKILL.md 中的**章节级/阶段级步骤**建立执行路线图即可。**对 SKILL 步骤的任何拆解、追踪、完成标记，必须且只能使用 `skill_step` 工具**
   | 调用方式 | 功能说明 |
   |---------|---------|
   | `skill_step(action="create", tasks=[...])` | 创建步骤列表（一次性创建所有章节级步骤） |
   | `skill_step(action="insert", idx=..., tasks=[...])` | 仅在确实需要时插入子步骤（见第 4 条） |
   | `skill_step(action="complete", idx=..., result="...")` | 完成单个步骤并记录结果 |
   | `skill_step(action="complete_batch", indices=[...], results=[...])` | 一次性收尾多个**已实际完成**的连续步骤；indices 必须严格升序、连续，且首项等于当前第一个未完成步 |
   | `skill_step(action="remove", idx=...)` | 移除步骤 |
   | `skill_step(action="list")` | 查看所有步骤 |
3. **严格顺序**：按 SKILL.md 定义的顺序逐步执行，**禁止跳过、合并或重排步骤**
4. **按需拆分**：满足以下任一条件时调用 `skill_step(action="insert")`：(a) 该步骤包含**多个独立、可单独失败**的子动作，需要分别跟踪进度；(b) 中间存在**需要单独向用户汇报**的产出节点；(c) 存在**需要单独把关**的风险/审批点。循环类步骤默认建一项即可，只有当每轮产物各自具有独立交付价值、需要单独留痕时才逐项建项。换句话说：只在拆分**真的能让执行更清晰**时才拆，避免为拆而拆
5. **优先批量收尾**：执行过程中**优先**使用 `skill_step(action="complete_batch")` 把若干已完成的连续步骤一次性收尾，减少工具往返；仅当某一步的产出需要单独说明、或后续步骤依赖该产出的具体描述时，才用 `skill_step(action="complete")`。**所有子步骤 skill_step 完成后才能标记该步骤为完成**。**严禁预先标记**：只能为已经实际执行完毕的步骤调用 complete/complete_batch；批量收尾时 results 必须为每一步独立填写，不得合并成一段总结
6. **闸门等待**：遇到需要用户确认/审批的步骤时，**必须等待用户回复，禁止自行假设用户同意**
7. **不确定时重读**：只能再次调用 `skill_tool`，**不得**用其它工具获取 SKILL.md
8. **内容忠实**：SKILL.md 是规格说明，不是参考建议。其中定义的选项列表、参数值、标签文本、推荐标记等必须**原样使用**，禁止自行添加、删除、修改或重新措辞
9. **错误处理**：执行子步骤出错时，**禁止自行决定跳过该步骤或后续步骤**。必须先尝试修复（如安装缺失依赖、修正参数），修复失败则询问用户如何处理，等待用户指示后再继续
10. **工具降级**：SKILL.md 中提到的工具如果在当前环境中不存在，必须先告知用户该工具不可用并说明你打算如何替代，获得用户同意后再继续。不要花时间反复检查工具列表
"""
    return """## Skill Execution Protocol (Mandatory)

The "Skills" section of this prompt (from SkillUseRail) lists available skills and how to load/release them — **follow that section**.

**Load SKILL.md body (never run tool names as shell/bash commands):**
- You **must** use **only** `skill_tool(skill_name=..., relative_file_path="SKILL.md")` to load the body; when the whole skill flow is done and you no longer need the body, call `skill_complete(skill_name=...)`.
- Only `skill_tool` enters the integrated path (session body copy, message protection, and `[ACTIVE SKILL BODY]` reinjection). **Do not** load or stitch SKILL.md with any other tool.
- If `skill_tool` is **not** in your available tool list, you cannot load skill bodies on this integration path—tell the user; **do not** substitute another file-reading tool.
- To see more or refresh the full SKILL.md, you **may only** call `skill_tool` again.

Then execute the workflow; the rules below govern execution.

1. **Declare step**: Before each action, state your current step at the start of your reply: `[Current Step: <step name>]`. **You do NOT call any tool to "start" a step** — the declaration itself enters the step.
2. **Create the roadmap**: After reading SKILL.md, call `skill_step(action="create", tasks=[...])` once with one item per **chapter-level / phase-level step** defined in the document. **Any breakdown, tracking, or completion of SKILL steps MUST use the `skill_step` tool exclusively.**
   | Call | Description |
   |-----------|-------------|
   | `skill_step(action="create", tasks=[...])` | Create the step list (one call, all chapter-level steps) |
   | `skill_step(action="insert", idx=..., tasks=[...])` | Insert sub-steps **only when actually needed** (see rule 4) |
   | `skill_step(action="complete", idx=..., result="...")` | Mark a single step complete and record the outcome |
   | `skill_step(action="complete_batch", indices=[...], results=[...])` | Close out several **already-finished** contiguous steps in one call. `indices` must be strictly ascending, contiguous, and start at the first open step. |
   | `skill_step(action="remove", idx=...)` | Remove a step |
   | `skill_step(action="list")` | View all steps |
3. **Strict order**: Execute steps in the exact order defined in SKILL.md. **Never skip, merge, or reorder steps.**
4. **Break down on demand**: Use `skill_step(action="insert")` when at least one of the following holds: (a) the step contains **multiple independent, separately-failable sub-actions** that need to be tracked individually; (b) there is an interim deliverable that **must be reported separately to the user**; (c) there is a risk / approval point that **needs separate gating**. For loop-style steps, default to a single skill_step; only enumerate per-iteration items when each iteration's output has independent delivery value and must be tracked separately. In short: split **only when splitting genuinely makes execution clearer** — do not split for the sake of splitting.
5. **Prefer batch completion**: During execution, **prefer** `skill_step(action="complete_batch")` to close out several finished contiguous steps in one call, reducing unnecessary tool round-trips. Use `skill_step(action="complete")` only when a particular step's output needs a dedicated description, or when subsequent steps depend on that specific description. **All sub-step skill_step items must be completed before marking the step as done.** **Never pre-mark**: only call complete/complete_batch for steps you have actually executed; in batch calls each `results` entry must be filled independently and must not be merged into a single summary.
6. **Gate enforcement**: When a step requires user confirmation/approval, **you MUST wait for the user's response. Never assume approval.**
7. **Re-read when unsure**: Refresh the SKILL.md body **only** by calling `skill_tool` again — **never** use any other tool to obtain SKILL.md.
8. **Content fidelity**: SKILL.md is a specification, not a suggestion. Option lists, parameter values, label text, and recommendation markers defined therein must be used **verbatim** — never add, remove, modify, or rephrase them.
9. **Error handling**: When a sub-step fails, **never decide on your own to skip it or subsequent steps**. First attempt to fix the issue (e.g. install missing dependencies, correct parameters). If the fix fails, ask the user how to proceed and wait for their instructions.
10. **Tool fallback**: If a tool mentioned in SKILL.md does not exist in your current environment, you MUST first inform the user that the tool is unavailable and explain how you plan to substitute it. Only proceed after the user agrees. Do not spend time repeatedly checking the tool list.
"""


class SkillProtocolPromptRail(DeepAgentRail):
    """每次 model_call 前刷新 skill_protocol 提示词段。"""

    priority = 8

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._registered_tools: list[Any] = []

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._register_session_toolkits(agent)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SKILL_PROTOCOL_SECTION_NAME)
            self.system_prompt_builder.remove_section(_SKILL_PLAN_REQUIRED_SECTION_NAME)
            self.system_prompt_builder.remove_section(_SKILL_COMPLETE_REQUIRED_SECTION_NAME)
        self.system_prompt_builder = None
        self._unregister_session_toolkits(agent)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        # Capture conversation_id for downstream before_model_call hook;
        # ctx.inputs only carries conversation_id at invoke time.
        conv_id = _extract_conversation_id(ctx)
        if conv_id:
            _session_id_var.set(conv_id)

    def _register_session_toolkits(self, agent) -> None:
        """注册 skill_step facade 到 agent，随 rail 生命周期上线/下线。"""
        if self._registered_tools:
            return
        try:
            from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
        except Exception as exc:
            logger.warning(
                "[SkillProtocolPromptRail] session toolkits import failed: %s", exc,
            )
            return

        registered: list[Any] = []
        ability_mgr = getattr(agent, "ability_manager", None)
        try:
            toolkit = SkillStepToolkit()
            for tool in toolkit.get_tools():
                if ability_mgr is not None:
                    existing = ability_mgr.get(tool.card.name)
                    if isinstance(existing, ToolCard):
                        ability_mgr.remove(tool.card.name)
                if not Runner.resource_mgr.get_tool(tool.card.id):
                    Runner.resource_mgr.add_tool(tool)
                if ability_mgr is not None:
                    ability_mgr.add(tool.card)
                registered.append(tool)
        except Exception as exc:
            logger.warning(
                "[SkillProtocolPromptRail] register skill_step facade failed: %s", exc,
            )

        self._registered_tools = registered
        logger.info(
            "[SkillProtocolPromptRail] session toolkits registered: tools=%s",
            [t.card.name for t in registered],
        )

    def _unregister_session_toolkits(self, agent) -> None:
        if not self._registered_tools:
            return
        ability_mgr = getattr(agent, "ability_manager", None)
        for tool in self._registered_tools:
            try:
                if ability_mgr is not None:
                    ability_mgr.remove(tool.card.name)
                Runner.resource_mgr.remove_tool(tool.card.id)
            except Exception as exc:
                logger.warning(
                    "[SkillProtocolPromptRail] unregister %s failed: %s",
                    tool.card.name, exc,
                )
        self._registered_tools = []

    def _resolve_priority(self, name: str, default_priority: int) -> int:
        existing = self.system_prompt_builder.get_section(name)
        return existing.priority if existing is not None else default_priority

    def _resolve_language(self) -> str:
        lang = getattr(self.system_prompt_builder, "language", None) or "cn"
        return "cn" if lang in ("cn", "zh") else "en"

    def _refresh_skill_phase_sections(
        self, language: str, session_id: Optional[str],
    ) -> None:
        """Inject/remove phase-driven mandatory sections.

        Phase → section mapping:
            WAITING_PLAN → skill_plan_required (must call skill_step action=create)
            DONE         → skill_complete_required (must call skill_complete)
            IDLE / IN_PROGRESS → no extra phase section (only the always-on
                                 skill_protocol section applies).
        """
        phase = get_session_phase(session_id) if session_id else SkillPhase.IDLE
        skill_name = get_session_active_skill(session_id) if session_id else None

        want_plan_required = phase == SkillPhase.WAITING_PLAN and bool(skill_name)
        want_complete_required = phase == SkillPhase.DONE and bool(skill_name)

        self._set_section(
            _SKILL_PLAN_REQUIRED_SECTION_NAME, want_plan_required,
            language, _build_skill_plan_required_text, skill_name,
        )
        self._set_section(
            _SKILL_COMPLETE_REQUIRED_SECTION_NAME, want_complete_required,
            language, _build_skill_complete_required_text, skill_name,
        )

    def _set_section(
        self, name: str, want: bool, language: str,
        builder, skill_name: Optional[str],
    ) -> None:
        if not want:
            try:
                self.system_prompt_builder.remove_section(name)
            except Exception as exc:
                logger.debug(
                    "[SkillProtocolPromptRail] remove %s section skipped: %s", name, exc,
                )
            return
        try:
            text = builder(language, skill_name)
            self.system_prompt_builder.add_section(PromptSection(
                name=name,
                content={language: text},
                priority=self._resolve_priority(name, _SKILL_PHASE_DIRECTIVE_PRIORITY),
            ))
        except Exception as exc:
            logger.warning(
                "[SkillProtocolPromptRail] build %s section failed: %s", name, exc,
            )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return

        language = self._resolve_language()
        # Latch conversation_id again in case before_invoke wasn't reached
        # (e.g. nested model calls within the same task).
        conv_id = _extract_conversation_id(ctx) or _session_id_var.get()
        self._refresh_skill_phase_sections(language, conv_id)

        try:
            protocol_text = _build_skill_protocol_section_text(language)
            self.system_prompt_builder.add_section(PromptSection(
                name=_SKILL_PROTOCOL_SECTION_NAME,
                content={language: protocol_text},
                priority=self._resolve_priority(
                    _SKILL_PROTOCOL_SECTION_NAME, PromptPriority.SKILL_PROTOCOL,
                ),
            ))
        except Exception as exc:
            logger.warning(
                "[SkillProtocolPromptRail] build skill_protocol section failed: %s", exc,
            )


__all__ = ["SkillProtocolPromptRail"]
