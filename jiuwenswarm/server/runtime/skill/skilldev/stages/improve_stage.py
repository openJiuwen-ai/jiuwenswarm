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

import logging

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

IMPROVE_SYSTEM_PROMPT = """你是一个 Skill 优化专家。根据用户反馈改进 Skill。

当前是第 {iteration} 轮迭代。

用户反馈：
{feedback}

评测报告：
{report}

当前 Skill 内容：
{skill_content}

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
        """调用 Agent 分析反馈并修改 skill 文件.

        待实现: 接入 create_stage_agent + Runner.run_agent，实现文件级改进
        """
        # 待实现:
        # skill_content = self._read_skill_files(ctx.workspace / "skill")
        # agent = ctx.create_stage_agent(
        #     stage_name="improve",
        #     system_prompt=IMPROVE_SYSTEM_PROMPT.format(
        #         iteration=ctx.state.iteration,
        #         feedback=json.dumps(feedback, ensure_ascii=False),
        #         report=report,
        #         skill_content=skill_content,
        #     ),
        #     tools=["file_read", "file_write"],
        #     max_iterations=25,
        # )
        # await Runner.run_agent(agent, {"task": "根据反馈改进 Skill"})
        logger.warning("[ImproveStage] _run_improve_agent 尚未实现，跳过改进")

    def _read_skill_files(self, skill_dir) -> str:
        """读取当前 skill 目录下所有文件内容."""
        parts = []
        for file_path in sorted(skill_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(skill_dir)
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                parts.append(f"=== {rel} ===\n{content}")
        return "\n\n".join(parts)
