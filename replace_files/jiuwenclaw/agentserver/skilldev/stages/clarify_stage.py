# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""CLARIFY 阶段处理器.

职责：
- 分析用户创建 Skill 的需求
- 生成 3-5 个结构化澄清问题，每题附带选项（允许自由输入）
- 将问题列表写入 state.clarification_questions
- 跳转到 QUESTION_CLARIFY 挂起点，等待用户回答

Agent 工具白名单：["file_read", "file_glob", "file_listdir"]
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.asset_utils import load_skill_content, load_asset_content, load_asset
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

CLARIFY_SYSTEM_PROMPT = """你是 Skill 需求澄清专家。目标是在进入实现前，用最少但关键的问题补齐信息缺口，为后续计划与实现降低返工风险。

## 工作方式（先判断再提问）

1. 先阅读用户需求与可用上下文，列出“已明确”与“仍不明确”信息。
2. 只对“不明确且会影响实现方案”的点提问；已明确内容禁止重复询问。
3. 按“对实现影响从高到低”排序问题，优先问会改变架构/工具/验收标准的决策点。

## 提问范围与禁止项

- **只问技能本身**：问题必须用于澄清 SKILL 的能力与行为——使用场景与边界、输入/输出与结果形态、流程与约束、与参考资料/外部工具如何协同、术语与歧义、可验证的验收标准等；不得把决策推给用户做「工程/项目管理」式选择。
- **禁止提问**（与技能内容无关的落盘、命名、文件关系类问题，一律不问，也不要换说法绕问）：
  - 文件或技能应保存到哪个路径、哪层目录、工作区内如何组织；
  - SKILL/技能目录或文件如何命名；
  - 是否覆盖、替换、备份、保留多版本、是否动「原始」文件、是否与已有 skill 二选一等。
- 若上述信息缺失，在合理默认下假设并继续；**不要**就上述禁止项向用户要确认。

## skill_name 澄清

若题目选项为 skill name 候选，则该题须遵守：
1. **选项即标识符**：每个选项的 `label` 必须是可直接使用的 skill_name 字符串本身，不得用中文描述或「中文名（英文标识）」混排。
2. **禁止中文与全角字符**：选项中不得出现任何汉字、中文标点或全角符号；不得含空格、下划线。
3. **格式**：kebab-case，仅小写字母、数字、短横线。
4. **语义**：2-4 个英文词为宜，总长建议不超过 30 字符；名称中不要出现 `skill` 字样。
5. **问题文案可用中文**，但选项 label 只能是符合上述规则的 ASCII 标识符。

## 优先澄清维度（按需选择，不必全问）

**意图与触发**
- Skill 的核心任务是什么（输入 -> 输出）？
- 在什么场景或用户措辞下触发？

**输出与验收**
- 期望输出形态（自然语言 / 结构化 / 文件）？
- 成功标准是否可客观验证（格式、准确性、覆盖率、约束）？

**能力边界与依赖**
- 是否依赖工具、MCP 或外部数据源？
- 是否有明确限制（时延、成本、安全、合规、权限）？

**知识与复杂度**
- 是否需要打包领域知识或参考资料？
- 复杂度预期（轻量单场景 vs 多场景/多步骤）？

## 出题规则

1. **最小必要**：输出 2-5 题，宁少勿多；每题都必须“有用且可决定实现方向”。
2. **单题单决策**：一个问题只聚焦一个决策点，避免把多个问题混在一起。
3. **选项可执行**：每题 2-4 个选项；选项应尽量互斥、覆盖主流路径、表达具体可落地。
4. **避免空泛**：不问“还有什么补充吗”这类泛问题。
5. **允许补充**：`allow_custom` 默认设为 `true`，除非该题确实不需要自定义输入。
6. **语言要求**：问题与选项使用中文，措辞简洁、无歧义；**skill_name 澄清题的选项 label 除外**，须为 ASCII kebab-case 标识符（见上文「skill_name 澄清」）。

## 输出格式（严格遵守）
仅输出 JSON 数组，不要输出任何解释、标题、代码块或其他 Markdown。

示例（仅示例结构，不要输出示例文字）：
[
  {
    "id": "q1",
    "question": "问题文本",
    "options": [
      {"id": "o1", "label": "选项1"},
      {"id": "o2", "label": "选项2"}
    ],
    "allow_custom": true
  }
]

格式约束：
- 问题 id：`q1`、`q2`、`q3`...
- 选项 id：每题内使用 `o1`、`o2`、`o3`...
- `question` 必须是完整问句
- 选项标签不超过 20 字
"""


class ClarifyStageHandler(StageHandler):
    """CLARIFY 阶段：生成澄清问题列表，跳转 QUESTION_CLARIFY 挂起点."""

    MAX_PARSE_RETRIES = 2

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在分析需求，生成澄清问题..."}
        )

        try:
            questions = await self._generate_questions(ctx)
            ctx.state.clarification_questions = questions
        except Exception as e:
            msg = f"澄清问题 生成失败：{e}"
            raise RuntimeError(msg) from e

        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {"message": f"已生成 {len(questions)} 个问题，等待用户回答"},
        )
        return StageResult(next_stage=SkillDevStage.QUESTION_CLARIFY)

    async def _generate_questions(self, ctx: SkillDevContext) -> list[dict]:
        """调用 Agent 生成问题列表 JSON."""
        agent = ctx.create_stage_agent(
            stage_name="clarify",
            system_prompt=CLARIFY_SYSTEM_PROMPT,
            tools=["file_read", "file_glob", "file_listdir"],
            max_iterations=30,
        )
        base_query = self._build_query(ctx)
        total_attempts = self.MAX_PARSE_RETRIES + 1

        for attempt in range(1, total_attempts + 1):
            query = base_query
            if attempt > 1:
                query = (
                    f"{base_query}\n\n"
                    "上一次输出未能解析为有效问题 JSON。请仅输出严格 JSON 数组，"
                    "不要输出解释、标题或 Markdown 代码块。"
                )

            raw_text = await ctx.run_stage_agent_streaming(
                agent, stage_name="clarify", query=query
            )
            logger.info(
                "[session=%s] [ClarifyStage] attempt=%s/%s raw output: %s",
                ctx.state.task_id,
                attempt,
                total_attempts,
                raw_text,
            )
            questions = self._parse_questions_json(raw_text, ctx.state.task_id)
            if questions is not None:
                logger.info(
                    "[session=%s] [ClarifyStage] attempt=%s/%s parse success, questions=%s",
                    ctx.state.task_id,
                    attempt,
                    total_attempts,
                    len(questions),
                )
                ctx.release_agent_tools(agent)
                return questions

            logger.warning(
                "[session=%s] [ClarifyStage] attempt=%s/%s parse failed",
                ctx.state.task_id,
                attempt,
                total_attempts,
            )

        logger.warning(
            "[session=%s] [ClarifyStage] all attempts failed, fallback to default questions. total_attempts=%s",
            ctx.state.task_id,
            total_attempts,
        )
        ctx.release_agent_tools(agent)
        return self._default_questions()

    def _build_query(self, ctx: SkillDevContext) -> str:
        """构建传给 Agent 的 query."""
        user_query = ctx.state.input.get("query", "")
        parts = [f"## 用户需求\n{user_query}"]

        asset_json = load_asset(ctx.workspace)
        has_preloaded_content = False

        if not ctx.state.skill_dir_empty:
            has_preloaded_content = True
            # 预加载skill内容
            skill_dir_str = asset_json.get("skill_dir", "")
            skill_dir = Path(skill_dir_str) if skill_dir_str else ctx.workspace / "skill"
            skill_content_str, skill_failed = load_skill_content(skill_dir=skill_dir)
            if skill_failed:
                logger.warning(
                    "[session=%s] [ClarifyStage] skill 文件部分未能预加载: %s",
                    ctx.state.task_id, skill_failed,
                )
            parts.append(
                "## 当前 Skill 内容\n"
                "用户对现有 Skill 提出了修改需求，以下为当前 Skill 文件的完整内容（已预加载，无需再调用工具读取）：\n\n"
                + (skill_content_str or "（未找到 skill 文件）")
            )
            parts.append("根据以上 Skill 内容生成澄清问题，须遵守系统提示中的「提问范围与禁止项」。")

            # 此模式不预加载用户上传的资料，agent自行判断后可加载
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                parts.append(f"工作区 {ctx.workspace} 中的resources/文件夹下存放了用户原始上传的参考资料，请根据需求自行判断是否需要查看。")
            if not ctx.state.tool_scripts_dir_empty:
                parts.append(f"用户提供了以下工具，可以在生成的skill中使用：\n{ctx.state.external_tools}")
        else:
            # 预加载用户上传的资料
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                content, _ = load_asset_content(
                    asset=asset_json,
                    include_ref_files=True,
                    include_ref_skills=True,
                    include_tools=False,
                )
                if content:
                    parts.append(
                        "## 参考资料\n"
                        f"以下为用户上传的参考资料，存放于工作区 {ctx.workspace} 中的resources/目录。"
                        f"现已预加载，无需再调用工具读取）：\n\n{content}"
                    )
                    has_preloaded_content = True

            if not ctx.state.tool_scripts_dir_empty:
                parts.append(
                    "## 外部工具\n"
                    "用户提供了以下可调用工具，可以在生成的Skill中使用：\n"
                    + str(ctx.state.external_tools)
                )
                has_preloaded_content = True
        if has_preloaded_content:
            parts.append(
                "## 约束\n1. 创建Skill可供参考的文件内容已尽量完整预加载。如预加载未覆盖某些文件，可酌情使用文件工具补充查看。\n"
                "2. 严禁主动读取其他文件内容。\n3. 请根据用户需求与参考资料，生成必要的关键澄清问题（JSON 数组格式）。"
            )
        else:
            parts.append("## 约束\n请根据用户需求，生成必要的澄清问题（JSON 数组格式）。严禁调用工具读取文件内容。")
        return "\n\n".join(parts)

    def _parse_questions_json(self, text: str, session_id: str) -> list[dict] | None:
        """从 Agent 输出中提取问题列表 JSON.

        优先提取 ```json ... ``` 代码块，否则用平衡括号匹配 JSON 数组。
        """
        # 先尝试提取代码块
        code_block = re.search(r"```(?:json)?\s*(\[.*?])\s*```", text, re.DOTALL)
        if code_block:
            try:
                questions = json.loads(code_block.group(1))
                if isinstance(questions, list):
                    return self._validate_questions(questions)
            except json.JSONDecodeError:
                pass

        # 平衡括号匹配 JSON 数组
        start = text.find("[")
        if start == -1:
            logger.warning("[session=%s] [ClarifyStage] Agent 未输出有效的问题 JSON 数组", session_id)
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
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        questions = json.loads(text[start: i + 1])
                        if isinstance(questions, list):
                            return self._validate_questions(questions)
                    except json.JSONDecodeError:
                        break

        logger.warning("[session=%s] [ClarifyStage] JSON 解析失败", session_id)
        return None

    def _validate_questions(self, questions: list) -> list[dict] | None:
        """校验并修复问题列表格式，确保字段完整."""
        validated = []
        for i, q in enumerate(questions):
            if not isinstance(q, dict):
                continue
            validated.append(
                {
                    "id": q.get("id", f"q{i + 1}"),
                    "question": q.get("question", ""),
                    "options": q.get("options", []),
                    "allow_custom": q.get("allow_custom", True),
                }
            )
        return validated if validated else None

    @staticmethod
    def _default_questions() -> list[dict]:
        """Agent 解析失败时的兜底问题列表."""
        return [
            {
                "id": "q1",
                "question": "这个 Skill 的核心任务与触发场景最接近哪种模式？",
                "options": [
                    {"id": "o1", "label": "编程辅助（代码类输入）"},
                    {"id": "o2", "label": "文档写作（文本类输入）"},
                    {"id": "o3", "label": "信息问答（检索类输入）"},
                    {"id": "o4", "label": "流程执行（多步任务）"},
                ],
                "allow_custom": True,
            },
            {
                "id": "q2",
                "question": "你期望 Skill 输出什么形式，并按什么标准算成功？",
                "options": [
                    {"id": "o1", "label": "结构化结果（可校验）"},
                    {"id": "o2", "label": "自然语言答案（可读性优先）"},
                    {"id": "o3", "label": "文件产物（代码/文档）"},
                    {"id": "o4", "label": "混合输出（文本+文件）"},
                ],
                "allow_custom": True,
            },
            {
                "id": "q3",
                "question": "这个 Skill 在能力边界与依赖上有哪些要求？",
                "options": [
                    {"id": "o1", "label": "纯推理，无外部依赖"},
                    {"id": "o2", "label": "需要工具/MCP 能力"},
                    {"id": "o3", "label": "依赖 resources 资料"},
                    {"id": "o4", "label": "有时延/成本/合规限制"},
                ],
                "allow_custom": True,
            },
        ]
