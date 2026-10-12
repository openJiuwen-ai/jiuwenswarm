# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillProtocolPromptRail — 注入「技能执行规范」提示词。

技能清单与 ``skill_tool`` 约定由上游 SkillUseRail 的 ``skills`` 段负责；本 rail 的
``skill_protocol`` 段与之对齐，只补充执行过程的强制规范。

「技能加速通道」由 ``SkillTurboPromptRail``（section ``skill_turbo_guide``）注入，
本段不重复该内容。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import PromptPriority

logger = logging.getLogger(__name__)

_SKILL_PROTOCOL_SECTION_NAME = "skill_protocol"


_CN_PROTOCOL = """## 技能执行规范（强制）

可用技能清单与加载约定见本 prompt 的「技能」段（SkillUseRail 注入），**须与该段一致**。

### 加载 SKILL.md 正文（禁止用 bash 执行工具名）
- **必须**且**只能**使用 `skill_tool(skill_name=..., relative_file_path="SKILL.md")` 加载。不要把工具名当作 shell 命令执行。
- **禁止**用 `read_file` 或任何其它工具读取、拼凑 SKILL.md。遇到技能名或 `.../<skill_name>/SKILL.md` 路径一律走 `skill_tool`；普通参考文件不受此限制。
- 加载**嵌套子技能**时，`skill_name` 仍填**顶层技能名**，子技能通过 `relative_file_path` 指定，例如 `skill_tool(skill_name="pptx-craft", relative_file_path="designer/SKILL.md")`。**不要**把子目录名当作 `skill_name`——它不是已注册技能，会返回 `Skill not found`。
- 若你当前可用工具列表中**没有** `skill_tool`：请向用户说明环境未开放该能力；**不得**用其它工具代替。
- 需要多看或刷新全文时，**只能**再次调用 `skill_tool`。
- **历史加载内容不跨任务复用**：历史对话轮次中 `skill_tool` 返回的 SKILL.md 内容仅对当时任务有效，**可能已过期**（技能可能已被卸载或更新）。新任务需要使用同一技能时，**必须**重新调用 `skill_tool` 确认技能仍然存在；若返回 `Skill not found`，说明技能已卸载，**禁止**继续使用历史轮次中的技能目录、脚本路径或 SKILL.md 内容执行任务，应告知用户该技能已不可用并征询后续处理方式。

随后按 SKILL 工作流执行；下列规范约束执行过程。

1. **声明步骤**：默认情况下，每次行动前必须在回复开头声明当前所在步骤，格式：`[当前步骤: <步骤名称>]`。**无需调用任何工具来"开始"步骤**——声明本身即代表进入该步。若 SKILL.md 明确声明“阶段状态和阶段消息由工具事件唯一生成”，则以该声明为准，禁止自行输出 `[当前步骤: ...]` 或其他步骤声明。
2. **必须使用 todo**：在执行 skill 步骤前，必须先创建 todo 列表。
   - **创建时搭便车**：`todo_create` **必须**和第一个工作工具在同一轮发出，禁止 `todo_create` 独占一轮。
   - **更新时搭便车（强制）**：`todo_modify` **必须**和下一个任务的工作工具在同一轮发出，禁止 `todo_modify` 独占一轮。系统会在工作工具被调用时自动将 pending 推进为 in_progress，你只需要用 `todo_modify` 标记 completed/cancelled。
   - ✅ 正确：`[write_file(...), todo_modify(action="update", todos=[{"id":"step1","status":"completed"}])]`
   - ❌ 禁止：`[todo_modify(action="update", todos=[{"id":"step1","status":"completed"}])]` ← 独占一轮，浪费 LLM 调用
   - 唯一例外：所有工作完成后、即将给出最终回复的最后一轮，可以单独调 `todo_modify`。
   - 可在连续推进多个任务后用一次 `todo_modify` 统一批量更新状态。
   放弃、跳过或决定不再执行某步骤时（如用户说「不生成 PPT 了」），**必须**立即 `todo_modify` 将该条标为 `cancelled`；
   禁止仅用口头回复收尾而仍保留 `in_progress`/`pending` 项。
3. **严格顺序**：按 SKILL.md 定义的顺序逐步执行，**禁止跳过、合并或重排步骤**，除非 SKILL.md 或用户明确允许。
4. **闸门等待**：遇到需要用户确认/审批的步骤时，**必须等待用户回复，禁止自行假设用户同意**。
5. **不确定时重读**：只能再次调用 `skill_tool`，**不得**用其它工具获取 SKILL.md。
6. **内容忠实**：SKILL.md 是规格说明，不是参考建议。其中定义的选项列表、参数值、标签文本、推荐标记等必须**原样使用**，禁止自行添加、删除、修改或重新措辞。
7. **错误处理**：执行子步骤出错时，**禁止自行决定跳过该步骤或后续步骤**。必须先尝试修复（如安装缺失依赖、修正参数），修复失败则询问用户如何处理，等待用户指示后再继续。
8. **工具降级**：SKILL.md 中提到的工具如果在当前环境中不存在，必须先告知用户该工具不可用并说明你打算如何替代，获得用户同意后再继续。不要花时间反复检查工具列表。
9. **用户打断后的处置**：按用户**原话**判定意图，**禁止自己猜**：
   - 原话含"继续"/"接着做"/"刚才那个继续" → 继续当前技能流程。
   - 其他情况（无关问题、闲聊、新任务、同技能新需求、显式取消、意图不明）→ 一律视为放弃原任务：
     先用 `todo_modify` 把所有 `in_progress`/`pending` 项标为 `cancelled`，再回应新请求。
     若新请求需要技能，重新用 `skill_tool` 加载并**重建** todo，不要沿用旧 todo。

⚠️ 用户发 N 条消息 ≠ N 个并发任务；新消息**默认覆盖**旧任务，**不追加**。禁止措辞："让我先完成之前的"/"先把之前的收尾"/"两个都做"/"先 X 再 Y"（除非用户原话已明示并列）。仅凭 history 有两条任务消息就推断"用户想做两个"是错误推理。
"""


_EN_PROTOCOL = """## Skill Execution Protocol (Mandatory)

The "Skills" section of this prompt (from SkillUseRail) lists available skills and how to load them — **follow that section**.

### Load SKILL.md body (never run tool names as shell/bash commands)
- You **must** use **only** `skill_tool(skill_name=..., relative_file_path="SKILL.md")` to load the body. Do not execute tool names as shell commands.
- **Never** use `read_file` or any other tool to read or stitch together SKILL.md. Whenever you see a skill name or a `.../<skill_name>/SKILL.md` path, go through `skill_tool`; ordinary reference files are not affected.
- To load a **nested sub-skill**, keep `skill_name` as the **top-level skill name** and point at the sub-skill via `relative_file_path`, e.g. `skill_tool(skill_name="pptx-craft", relative_file_path="designer/SKILL.md")`. **Do not** pass the sub-directory name as `skill_name` — it is not a registered skill and will return `Skill not found`.
- If `skill_tool` is **not** in your available tool list, tell the user the capability is not enabled in this environment; **do not** substitute another file-reading tool.
- To see more or refresh the full SKILL.md, you **may only** call `skill_tool` again.
- **Loaded content does not carry across tasks**: SKILL.md content returned by `skill_tool` in earlier conversation turns is only valid for that task and **may be stale** — the skill may have been uninstalled or updated. Whenever a new task needs the same skill, you **must** call `skill_tool` again to confirm the skill still exists; if it returns `Skill not found`, the skill has been uninstalled. In that case you **must not** continue using the skill's directory, script paths or SKILL.md content from earlier turns — inform the user the skill is unavailable and ask how to proceed.

Then execute the workflow; the rules below govern execution.

1. **Declare step**: By default, before each action, state your current step at the start of your reply: `[Current Step: <step name>]`. **You do NOT call any tool to "start" a step** — the declaration itself enters the step. If SKILL.md explicitly states that stage status and stage messages are emitted exclusively by tool events, follow that rule and you must not declare `[Current Step: ...]` or any other step message yourself.
2. **Use todo (mandatory)**: For skills, you MUST create a todo list before executing the skill steps.
   - **Piggyback on creation**: `todo_create` **MUST** be called in the same response as the first work tool — never in a standalone todo-only round.
   - **Piggyback on updates (MANDATORY)**: `todo_modify` **MUST** be called alongside the next task's work tool in the same response — never alone. The system auto-advances pending tasks to in_progress when work tools are called, so you only need `todo_modify` to mark tasks completed/cancelled.
   - ✅ CORRECT: `[write_file(...), todo_modify(action="update", todos=[{"id":"step1","status":"completed"}])]`
   - ❌ PROHIBITED: `[todo_modify(action="update", todos=[{"id":"step1","status":"completed"}])]` ← wastes an entire LLM round
   - The ONLY exception: the very final round when all work is done and you are about to give the final answer.
   - You may run several tasks back-to-back and update statuses in a single batched `todo_modify`.
   When abandoning or skipping a step (e.g. the user says not to generate the PPT), you **must** call
   `todo_modify` to mark it `cancelled` immediately; never end with text only while items stay
   `in_progress` or `pending`.
3. **Strict order**: Execute steps in the order defined by SKILL.md. **Do not skip, merge, or reorder steps** unless SKILL.md or the user explicitly allows it.
4. **Gate enforcement**: When a step requires user confirmation/approval, **you MUST wait for the user's response. Never assume approval.**
5. **Re-read when unsure**: Refresh the SKILL.md body **only** by calling `skill_tool` again — **never** use any other tool to obtain SKILL.md.
6. **Content fidelity**: SKILL.md is a specification, not a suggestion. Option lists, parameter values, label text, and recommendation markers defined therein must be used **verbatim** — never add, remove, modify, or rephrase them.
7. **Error handling**: When a sub-step fails, **never decide on your own to skip it or subsequent steps**. First attempt to fix the issue (e.g. install missing dependencies, correct parameters). If the fix fails, ask the user how to proceed and wait for their instructions.
8. **Tool fallback**: If a tool mentioned in SKILL.md does not exist in your current environment, you MUST first inform the user that the tool is unavailable and explain how you plan to substitute it. Only proceed after the user agrees. Do not spend time repeatedly checking the tool list.
9. **Handling user interruption**: Decide intent strictly from the user's **literal words**, **never guess**:
   - Words include "continue" / "go on" / "resume the previous one" → continue the current skill flow.
   - Anything else (unrelated question, small talk, a new task, a new requirement for the same skill,
     explicit cancellation, unclear intent) → treat the original task as abandoned: first call
     `todo_modify` to mark every `in_progress`/`pending` item `cancelled`, then respond to the new request.
     If the new request needs a skill, load it again via `skill_tool` and **rebuild** the todo list
     instead of reusing the old one.

⚠️ N user messages ≠ N parallel tasks; a new substantive message **replaces** the prior request by default — it does NOT stack. Forbidden phrasing: "let me finish the previous task first" / "let me wrap that up" / "let me do both" / "first X then Y" (unless the user's own words explicitly stated parallel intent). Inferring multiple parallel tasks purely from history is a **wrong inference** that MUST be avoided.
"""


def _build_skill_protocol_section_text(language: str) -> str:
    return _CN_PROTOCOL if language == "cn" else _EN_PROTOCOL


class SkillProtocolPromptRail(DeepAgentRail):
    """Refresh the skill_protocol prompt section before each model call."""

    priority = 8

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder: Any = None

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        # 热重载后 agent.system_prompt_builder 可能已是新引用，退休清理前先同步缓存，
        # 确保 remove_section 落到当前生效的 builder 上。
        _builder = getattr(agent, "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SKILL_PROTOCOL_SECTION_NAME)
        self.system_prompt_builder = None

    def _resolve_priority(self, name: str, default_priority: int) -> int:
        if self.system_prompt_builder is None:
            return default_priority
        existing = self.system_prompt_builder.get_section(name)
        return existing.priority if existing is not None else default_priority

    def _resolve_language(self) -> str:
        lang = getattr(self.system_prompt_builder, "language", None) or "cn"
        return "cn" if lang in ("cn", "zh") else "en"

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        # 热重载会新建 SystemPromptBuilder 并替换 agent.system_prompt_builder，但保留型
        # rail 不会重新 init()，缓存的 self.system_prompt_builder 可能指向旧 builder。
        # 这里每次从 ctx.agent 现取最新 builder 并刷新缓存。
        _builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder

        if self.system_prompt_builder is None:
            return

        language = self._resolve_language()
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
