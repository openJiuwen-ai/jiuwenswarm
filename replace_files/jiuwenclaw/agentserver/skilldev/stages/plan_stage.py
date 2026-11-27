# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PLAN 阶段处理器.

职责：
- 综合用户原始需求 + QUESTION_CLARIFY 阶段收集的 QA 答案
- 调用 Agent 生成结构化 JSON 开发计划
- 将 plan 写入 state，直接跳转到 GENERATE（无需用户二次确认）

Agent 工具白名单：["file_read", "file_glob", "file_listdir"]
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.asset_utils import load_asset, load_asset_content, load_skill_content
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是 Skill 架构师。你的目标是：基于“用户原始需求 + 澄清问答”，输出一份可直接进入生成阶段的高质量 JSON 开发计划。

## 工作方法（先分析，再输出）
1. 先提炼需求事实：能力目标、触发场景、输入输出、验收方式。
2. 再识别关键决策：目录拆分、工具依赖、边界限制、测试策略。
3. 最后输出唯一 JSON 对象；禁止解释性文字。

## 输入优先级
用户补充信息（澄清问答）优先级最高，必须体现在最终各字段中。

## 必须覆盖的分析点
- **能力与触发**：Skill 要解决什么问题，哪些用户意图/措辞应触发。
- **输出与验收**：输出形态、格式约束、是否可自动化验证。
- **约束与依赖**：时延/成本/安全/权限限制，外部工具或 MCP 依赖。
- **知识与资源**：是否需要 references/，是否需要 scripts/ 承载重复性确定步骤。
- **复杂度评估**：给出 low/medium/high，并与方案复杂度一致。

## 目录规划原则
- `SKILL.md` 必须存在。
- 出现重复、确定性流程时，优先放入 `scripts/`。
- 大段领域知识、示例、规范文档放入 `references/`，避免 `SKILL.md` 过长。
- 模板/图标/字体等资源放入 `assets/`。
- `SKILL.md` 目标 < 500 行，超过应拆分并在 plan 中说明查阅时机。

## description 写法要求
- 使用祈使句，描述“何时触发 + 做什么”。
- 触发描述应具体，覆盖典型场景，避免欠触发。
- 不写实现细节，不写空泛口号。

## 输出要求（严格）
- 只输出一个 JSON 对象。
- 不要 Markdown、不要代码块、不要前后解释。
- 字段必须完整，键名必须与下方结构一致。

输出结构示例（语义可变，结构不可变）：
{
  "skill_name": "kebab-case 标识名",
  "display_name": "用户可见名称",
  "description": "触发描述——用祈使句，覆盖触发场景，稍微'激进'以避免欠触发",
  "purpose": "这个 skill 解决什么问题",
  "intent_capture": {
    "what": "Skill 赋予模型的能力",
    "when": "触发场景",
    "output_format": "预期输出格式",
    "testable": true
  },
  "directory_structure": {
    "SKILL.md": "主指令文件",
    "scripts/xxx.py": "文件职责说明（如有需要）"
  },
  "key_decisions": [
    "决策1：为什么选择 X 而不是 Y"
  ],
  "test_strategy": {
    "approach": "测试方法描述",
    "test_cases_outline": ["场景1", "场景2", "场景3"]
  },
  "estimated_complexity": "low | medium | high"
}
"""


class PlanStageHandler(StageHandler):
    """PLAN 阶段：综合 QA 答案生成开发计划，直接进入 GENERATE."""

    MAX_PARSE_RETRIES = 2

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在根据需求和您的回答生成开发计划..."}
        )

        plan = await self._generate_plan(ctx)
        plan["skill_name"] = ctx.state.skill_name
        ctx.state.plan = plan

        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "开发计划已生成，即将开始生成 Skill 文件"}
        )
        return StageResult(next_stage=SkillDevStage.GENERATE)

    async def _generate_plan(self, ctx: SkillDevContext) -> dict:
        """调用 Agent 生成 plan JSON."""
        agent = ctx.create_stage_agent(
            stage_name="plan",
            system_prompt=PLAN_SYSTEM_PROMPT,
            tools=["file_read", "file_glob", "file_listdir"],
            max_iterations=30,
        )
        base_query = self._build_plan_query(ctx)
        total_attempts = self.MAX_PARSE_RETRIES + 1
        plan = None

        for attempt in range(1, total_attempts + 1):
            query = base_query
            if attempt > 1:
                query = (
                    f"{base_query}\n\n"
                    "上一次输出未能解析为有效 JSON 对象。请仅输出严格 JSON 对象，"
                    "不要输出解释、标题或 Markdown 代码块。"
                )
            plan_text = await ctx.run_stage_agent_streaming(
                agent, stage_name="plan", query=query
            )
            logger.info(
                "[session=%s] [PlanStage] attempt=%s/%s plan_text length: %d",
                ctx.state.task_id,
                attempt,
                total_attempts,
                len(plan_text),
            )
            plan = self._parse_plan_json(plan_text, ctx.state.task_id)
            if plan is not None:
                logger.info(
                    "[session=%s] [PlanStage] attempt=%s/%s parse success",
                    ctx.state.task_id,
                    attempt,
                    total_attempts,
                )
                break
            logger.warning(
                "[session=%s] [PlanStage] attempt=%s/%s parse failed",
                ctx.state.task_id,
                attempt,
                total_attempts,
            )

        if plan is None:
            logger.warning(
                "[session=%s] [PlanStage] all attempts failed, fallback to default plan. total_attempts=%s",
                ctx.state.task_id,
                total_attempts,
            )
            plan = self._default_plan(ctx)
        ctx.release_agent_tools(agent)
        return plan

    def _build_plan_query(self, ctx: SkillDevContext) -> str:
        """构造 Agent query，综合原始需求 + QA 答案."""
        user_query = ctx.state.input.get("query", "")
        parts = [f"## 用户需求\n{user_query}"]

        # 将 QA 问答内容以可读形式注入
        if ctx.state.clarification_questions and ctx.state.clarification_answers:
            q_map = {q["id"]: q["question"] for q in ctx.state.clarification_questions}
            qa_lines = [
                f"- {q_map.get(a['question_id'], a['question_id'])}: {a['answer']}"
                for a in ctx.state.clarification_answers
            ]
            parts.append("## 用户补充信息（澄清问答）\n" + "\n".join(qa_lines))
        elif ctx.state.clarification_answers:
            qa_lines = [
                f"- {a['question_id']}: {a['answer']}"
                for a in ctx.state.clarification_answers
            ]
            parts.append("## 用户补充信息\n" + "\n".join(qa_lines))

        asset_json = load_asset(ctx.workspace)
        has_preloaded_content = False
        if not ctx.state.skill_dir_empty:
            has_preloaded_content = True
            # 预加载当前 skill 内容
            skill_dir_str = asset_json.get("skill_dir", "")
            skill_dir = Path(skill_dir_str) if skill_dir_str else ctx.workspace / "skill"
            skill_content_str, skill_failed = load_skill_content(skill_dir=skill_dir)
            if skill_failed:
                logger.warning(
                    "[session=%s] [PlanStage] skill 文件部分未能预加载: %s",
                    ctx.state.task_id, skill_failed,
                )
            skill_content_str = skill_content_str or "（未找到 skill 文件）"
            parts.append(
                "## 当前 Skill 内容\n"
                f"工作区 {ctx.workspace} 中的skill文件夹下存放了已经生成的 SKILL.md，"
                f"以下为当前 Skill 文件的完整内容（已预加载，无需再调用工具读取）请先阅读此skill再做规划：\n\n"
                f"{skill_content_str}"
            )
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                parts.append(f"工作区 {ctx.workspace} 中的resources/文件夹下存放了用户原始上传的参考资料，请根据需求自行判断是否需要查看。")
            if not ctx.state.tool_scripts_dir_empty:
                parts.append(f"用户提供了以下工具，可以在生成的skill中使用：\n{ctx.state.external_tools}")
        else:
            # 预加载参考资料
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                content, _ = load_asset_content(
                    asset=asset_json,
                    include_ref_files=True,
                    include_ref_skills=True,
                    include_tools=False,
                )
                if content:
                    parts.append(
                        f"以下为用户上传的参考资料，存放于工作区 {ctx.workspace} 中的resources/目录。"
                        f"现已预加载，无需再调用工具读取）：\n\n{content}"
                        "请阅读以上预加载的资料后再规划"
                    )
                    has_preloaded_content = True

            if not ctx.state.tool_scripts_dir_empty:
                parts.append(
                    "## 外部工具\n"
                    "用户提供了以下可调用工具，Skill 可在执行时使用：\n"
                    + str(ctx.state.external_tools)
                )
                has_preloaded_content = True
        if has_preloaded_content:
            parts.append(
                "以上文件内容已尽量完整预加载。如预加载未覆盖某些文件，可酌情使用文件工具补充查看。\n"
                "请根据以上信息，输出一份完整的 JSON 开发计划。"
            )
        return "\n\n".join(parts)

    def _parse_plan_json(self, text: str, session_id: str) -> dict | None:
        """从 Agent 输出中提取 JSON plan.

        优先提取 ```json ... ``` 代码块，否则用平衡括号匹配 JSON 对象。
        """
        code_block = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
        if code_block:
            try:
                return json.loads(code_block.group(1))
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        if start == -1:
            logger.warning("[session=%s] [PlanStage] Agent 未输出有效的 JSON plan 对象", session_id)
            return None

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: i + 1])
                    except json.JSONDecodeError:
                        break

        end = text.rfind("}") + 1
        if end == 0:
            logger.warning("[session=%s] [PlanStage] Agent 未输出有效的 JSON plan 对象", session_id)
            return None
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            logger.warning("[session=%s] [PlanStage] JSON 解析失败", session_id)
            return None

    def _default_plan(self, ctx: SkillDevContext) -> dict:
        """Plan 解析连续失败时的兜底计划，确保后续阶段可执行."""
        return {
            "skill_name": ctx.state.skill_name or "generated-skill",
            "display_name": "Generated Skill",
            "description": "根据用户需求生成并完善 Skill 文件。",
            "purpose": ctx.state.input.get("query", "基于需求生成可执行 Skill"),
            "intent_capture": {
                "what": "将用户需求转化为可执行的 Skill 产物",
                "when": "用户请求创建或修改 Skill 时",
                "output_format": "SKILL.md 及必要辅助文件",
                "testable": True,
            },
            "directory_structure": {
                "SKILL.md": "主指令文件，定义触发场景、执行步骤和输出约束",
            },
            "key_decisions": [
                "优先保证最小可执行交付，先生成 SKILL.md",
                "在信息不完整时采用可扩展结构，后续可增量补充",
            ],
            "test_strategy": {
                "approach": "先进行结构与格式校验，再做核心场景验证",
                "test_cases_outline": [
                    "基础触发场景可执行",
                    "输出格式符合约束",
                    "边界输入下行为稳定",
                ],
            },
            "estimated_complexity": "medium",
        }
