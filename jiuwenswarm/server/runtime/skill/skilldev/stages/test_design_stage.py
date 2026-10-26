# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TEST_DESIGN 阶段处理器.

对齐官方 skill-creator 的测试设计流程：

1. 先生成 2-3 个真实用户场景的 test prompts（不写 assertions）
2. Assertions 应在 TEST_RUN 阶段运行期间并行起草
   （当前框架简化：在本阶段一次性生成 prompts + assertions）

evals.json 格式对齐官方 references/schemas.md：
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "files": [],
      "expectations": ["The output includes X", "The skill used script Y"]
    }
  ]
}
"""

from __future__ import annotations

import json
import logging

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

TEST_DESIGN_SYSTEM_PROMPT = """根据以下 Skill 内容，设计 {count} 个测试用例。

## 测试用例设计原则

### prompt 要求（对齐官方标准）
- 模拟真实用户输入：包含文件路径、个人背景、具体数据名称等细节
- 混合不同长度和表达风格（正式/随意/简短/详细）
- 覆盖不同复杂度和边缘场景
- 有些用户不会明确提到 skill 名称，但确实需要这个 skill 的功能

### expectations（assertions）要求
- 每条 expectation 是一个可客观验证的声明（字符串）
- 使用描述性名称，让阅读者一眼理解检查的内容
- 好的 expectation 是 *区分性的*：使用 skill 时通过，不使用时大概率失败
- 避免太容易通过的检查（如只检查文件名存在，不检查内容）
- 主观性输出（写作风格、设计质量）更适合人工评审，不强加 expectations

### 输出 JSON 格式（对齐官方 evals.json schema）
{{
  "skill_name": "{skill_name}",
  "evals": [
    {{
      "id": 1,
      "prompt": "模拟用户的真实输入...",
      "expected_output": "预期结果的人类可读描述",
      "files": [],
      "expectations": [
        "输出中包含 X 的结构化数据",
        "使用了 scripts/ 中的 Y 脚本"
      ]
    }}
  ]
}}
"""


class TestDesignStageHandler(StageHandler):
    """TEST_DESIGN 阶段：Agent 设计测试用例，输出 evals.json."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "正在设计测试用例..."})

        skill_content = self._read_skill_files(ctx.workspace / "skill")
        evals = await self._design_evals(ctx, skill_content)

        ctx.state.evals = evals
        evals_file = ctx.workspace / "evals" / "evals.json"
        evals_file.write_text(
            json.dumps(evals, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        count = len(evals.get("evals", []))
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": f"已设计 {count} 个测试用例"}
        )
        return StageResult(next_stage=SkillDevStage.TEST_RUN)

    def _read_skill_files(self, skill_dir) -> str:
        """读取 skill 目录下所有文件，拼接为字符串供 Agent 分析."""
        parts = []
        for file_path in sorted(skill_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(skill_dir)
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    parts.append(f"=== {rel} ===\n{content}")
                except Exception as exc:
                    logger.warning(
                        "[TestDesignStage] 读取文件失败: %s (%s)", file_path, exc
                    )
        return "\n\n".join(parts)

    async def _design_evals(self, ctx: SkillDevContext, skill_content: str) -> dict:
        """调用 Agent 设计测试用例.

        待实现: 接入 create_stage_agent + Runner.run_agent，解析输出 JSON
        """
        # 待实现:
        # agent = ctx.create_stage_agent(
        #     stage_name="test_design",
        #     system_prompt=TEST_DESIGN_SYSTEM_PROMPT.format(count=3),
        #     tools=[],  # 只需模型推理，无需工具
        #     max_iterations=10,
        # )
        # result = await Runner.run_agent(agent, {"skill_content": skill_content})
        # return json.loads(result["output"])

        logger.warning("[TestDesignStage] _design_evals 尚未实现，返回占位测试用例")
        skill_name = (
            ctx.state.plan.get("skill_name", "skill") if ctx.state.plan else "skill"
        )
        return {
            "skill_name": skill_name,
            "evals": [
                {
                    "id": 1,
                    "name": "basic-usage",
                    "prompt": f"请使用 {skill_name} 完成基础功能测试",
                    "expected_output": "待实现: 预期结果",
                    "files": [],
                    "expectations": ["待实现: 可验证的预期声明"],
                }
            ],
        }
