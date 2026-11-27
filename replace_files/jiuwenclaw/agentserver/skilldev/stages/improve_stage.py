# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""IMPROVE 阶段处理器.

职责：
- 读取用户最新反馈（feedback_history[-1]）和评测报告
- 创建 IMPROVE 专属 ReActAgent（配备文件读写工具 + 改进 Prompt）
- Agent 分析反馈，改进 skill/ 目录下的文件
- iteration 计数 +1，跳转回 TEST_RUN 开启新一轮测试

改进原则（写入 Prompt）：
1. 从反馈中提炼通用改进，不过拟合到特定测试用例
2. 保持指令精简，删除无效内容
3. 解释 why 而非堆砌 MUST/NEVER
4. 关注 benchmark 中的异常模式

Agent 工具白名单：["file_read", "file_write"]
"""

from __future__ import annotations
import json
import logging
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.utils.skill_description_fix import (
    fix_skill_md_description,
)

logger = logging.getLogger(__name__)

IMPROVE_SYSTEM_PROMPT = """你是一个 Skill 优化专家。根据用户反馈改进 Skill。

当前是第 {iteration} 轮迭代。

用户反馈：
{feedback}

评测报告：
{report}

## 首要任务：读取当前 Skill 文件

工作区路径：{workspace}
Skill 文件存储于工作区的 `skill/` 子目录下。

**请在开始改进之前，先使用文件工具读取 skill/ 目录下的全部内容：**
1. 用 `list_directory` 或 `glob` 列出 `skill/` 目录下所有文件
2. 用 `read_file` 依次读取各文件（从 `skill/SKILL.md` 开始）
3. 在充分理解现有内容后，再根据反馈做出有针对性的改进

## 改进哲学（对齐官方 skill-creator 指导）

### 1. 从反馈中泛化，不要过拟合
你在极少数示例上迭代，但 Skill 需要在海量不同场景中表现良好。
不要为特定测试用例添加琐碎的过拟合修改或限制性的 MUST 规则。
尝试理解用户反馈背后的 *根本意图*，将理解注入到指令中。

### 2. 保持精简，删除无效内容
阅读测试的 transcripts（不仅是最终输出）——如果 Skill 让模型在不产出价值的步骤上
浪费大量时间，删除引起这些行为的 Skill 指令并观察效果。

### 3. 解释 why，用心智模型替代死板规则
当今的 LLM 足够智能。与其写 "ALWAYS do X" 或 "NEVER do Y"，
不如解释 *为什么* X 重要、为什么 Y 会导致问题。
让模型理解意图后自主决策，比死板规则更有效、更优雅。

### 4. 发现重复工作 → 捆绑脚本
阅读测试运行的 transcripts，如果所有测试用例都独立编写了类似的辅助脚本
（如 create_docx.py、build_chart.py），这是强烈信号：
应将该脚本写好放入 scripts/，让每次调用直接使用而非重新发明。

### 5. 关注 Benchmark 异常模式
- 某 assertion 在所有配置都 pass → 可能不具区分力，考虑加强或替换
- 某 assertion 在所有配置都 fail → 可能超出能力范围或 assertion 本身有问题
- 高方差 eval → 可能是 flaky 测试或非确定性行为
- with_skill 反而劣于 baseline 的指标 → Skill 可能在某方面产生负面影响

### 6. 先写草稿，再以新鲜眼光审视
写完改进后，以全新视角审视一遍。如果某个持续性问题用当前方法解决不了，
尝试换一种思路——不同的隐喻、不同的工作模式、不同的文件组织方式。
尝试成本低，或许能找到突破口。

请输出改进后的完整文件内容。

## 文件范围约束
你只能生成与 Skill 本身直接相关的文件（例如 SKILL.md、scripts/、references/、assets/）。
禁止生成与 Skill 交付无关的文件，例如实现总结、README、CHANGELOG、开发说明、复盘文档等。

## 原则性要求
请务必将文件写入 skill/ 目录下（如 skill/SKILL.md），并确保 YAML frontmatter 格式正确（name 为 kebab-case）。
frontmatter 中的 `description` 必须是单行纯文本，禁止以 `>`、`-`、`*`、`#` 等 Markdown 标记开头，禁止使用 YAML 块标量（`>-`、`|` 等）。

## 工作区
当前工作区路径为：{workspace}
Skill 的所有文件存储于其中的 skill/ 子目录下。
根据用户的需求自行判断是否需要查看工作区中的已有文件，
在已有内容基础上进行修改或补全，避免覆盖不需要变更的部分。
如果当前任务是优化已有 skill，且已有 skill 位于 skill/ 目录下，
请先将原始 skill/ 目录重命名为 skill-vx/（x 表示递增版本号），
再新建一个全新的 skill/ 目录，并将本次生成的完整 Skill 文件保存到新的 skill/ 目录下。
"""


class ImproveStageHandler(StageHandler):
    """IMPROVE 阶段：Agent 根据用户反馈改进 Skill，随后进入下一轮测试."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        if not ctx.state.feedback_history:
            raise ValueError("IMPROVE 阶段缺少反馈历史，请先完成 REVIEW 阶段")

        latest_feedback = ctx.state.feedback_history[-1].get("feedback", {})
        report = (ctx.state.eval_results or {}).get("report", "")

        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {
                "message": f"正在根据反馈进行第 {ctx.state.iteration + 1} 轮改进...",
            },
        )

        await self._run_improve_agent(ctx, latest_feedback, report)

        ctx.state.iteration += 1
        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {
                "message": f"改进完成，开始第 {ctx.state.iteration} 轮测试",
            },
        )
        return StageResult(next_stage=SkillDevStage.TEST_RUN)

    async def _run_improve_agent(
        self, ctx: SkillDevContext, feedback: dict, report: str
    ) -> None:
        """调用 Agent 分析反馈并修改 skill 文件（流式，实时推送 LLM 推理过程）."""
        agent = ctx.create_stage_agent(
            stage_name="improve",
            system_prompt=IMPROVE_SYSTEM_PROMPT.format(
                iteration=ctx.state.iteration,
                feedback=json.dumps(feedback, ensure_ascii=False),
                report=report,
                workspace=ctx.workspace,
            ),
            tools=["file_read", "file_write", "file_edit", "file_glob", "file_grep", "shell"],
            max_iterations=50,
        )
        await ctx.run_stage_agent_streaming(
            agent, stage_name="improve", query="根据反馈改进 Skill"
        )
        skill_md_path = ctx.workspace / "skill" / "SKILL.md"
        if skill_md_path.exists():
            fix_skill_md_description(skill_md_path)
        ctx.release_agent_tools(agent)
