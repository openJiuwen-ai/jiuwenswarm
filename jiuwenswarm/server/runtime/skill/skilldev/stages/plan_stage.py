# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PLAN 阶段处理器.

职责：
- 创建 PLAN 专属 ReActAgent（配备搜索工具 + 规划 Prompt）
- Agent 分析需求，输出结构化的 JSON 开发计划
- 将 plan 写入 state，跳转到 PLAN_CONFIRM 挂起点
- Pipeline 在挂起点自动推送 CONFIRM_REQUEST 弹框（含 plan 数据）

Agent 工具白名单：["web_search"]（禁止文件写入，PLAN 阶段只规划不执行）
"""

from __future__ import annotations

import json
import logging

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个 Skill 架构师。根据用户需求，设计一份结构化的 Skill 开发计划。

## 第一步：Capture Intent（需求理解）

在设计之前，先从需求中提取以下关键信息：
1. 这个 Skill 要让模型能做什么？
2. 什么用户场景/措辞应触发这个 Skill？
3. 预期的输出格式是什么？
4. 输出是否可客观验证（适合自动化测试），还是主观的（更适合人工评审）？

## 第二步：Interview & Research（深入调研）

主动识别并记录：
- 边缘案例
- 输入/输出格式约束
- 依赖工具或 MCP
- 成功标准
- 可能的领域知识来源

## 第三步：输出 JSON Plan

```json
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
    "scripts/xxx.py": "文件职责说明"
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
```

## 设计原则

### 目录结构决策
- 有重复性确定步骤 → 放 scripts/（每次调用省去重新发明轮子）
- 有领域知识文档 → 放 references/（按需加载，不膨胀主文件）
- 有模板/图标/字体 → 放 assets/（输出时直接引用）
- SKILL.md 目标 <500 行；超过则拆分到 references/ 并标明查阅时机

### 描述的触发性
当前模型倾向于不够主动触发 Skill。description 应略微"推进式"：
- 除了说明功能，还要列举具体使用场景
- 即使用户没有明确提到 skill 名称也应触发
- 对标相似能力的区分点

### 修改模式
如果是修改已有 skill，先分析现有结构的优劣，plan 侧重差量而非全量重写。
"""


class PlanStageHandler(StageHandler):
    """PLAN 阶段：Agent 生成开发计划，随后进入 PLAN_CONFIRM 挂起点."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在分析需求并生成开发计划..."}
        )

        plan = await self._generate_plan(ctx)
        ctx.state.plan = plan

        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "开发计划已生成，等待确认"}
        )
        return StageResult(next_stage=SkillDevStage.PLAN_CONFIRM)

    async def _generate_plan(self, ctx: SkillDevContext) -> dict:
        """调用 ReActAgent 生成 plan JSON.

        待实现: 接入 create_stage_agent + Runner.run_agent，流式推送 AGENT_THINKING 事件
        """
        # 待实现:
        # agent = ctx.create_stage_agent(
        #     stage_name="plan",
        #     system_prompt=PLAN_SYSTEM_PROMPT,
        #     tools=["web_search"],
        #     max_iterations=15,
        # )
        # messages = self._build_messages(ctx)
        # plan_text = ""
        # async for chunk in agent.stream(messages):
        #     await ctx.emit(SkillDevEventType.AGENT_THINKING, {"delta": chunk.content})
        #     plan_text += chunk.content
        # plan = self._parse_plan_json(plan_text)
        # if ctx.state.existing_skill_md:
        #     plan["diff_analysis"] = "待实现: 差量分析"
        # return plan

        logger.warning("[PlanStage] _generate_plan 尚未实现，返回占位 plan")
        query = ctx.state.input.get("query", "")
        return {
            "skill_name": "placeholder-skill",
            "display_name": "占位 Skill",
            "description": f"根据需求『{query}』生成的 skill（待实现）",
            "purpose": "待实现",
            "directory_structure": {"SKILL.md": "主指令文件"},
            "key_decisions": [],
            "test_strategy": {"approach": "待实现", "test_cases_outline": []},
            "estimated_complexity": "medium",
        }

    def _build_messages(self, ctx: SkillDevContext) -> list[dict]:
        """构造发送给 PLAN Agent 的消息列表."""
        query = ctx.state.input.get("query", "")
        parts = [f"需求：{query}"]

        if ctx.state.reference_texts:
            refs = "\n\n".join(ctx.state.reference_texts[:3])  # 限制上下文长度
            parts.append(f"参考资料：\n{refs}")

        if ctx.state.existing_skill_md:
            parts.append(f"已有 SKILL.md：\n{ctx.state.existing_skill_md}")

        return [{"role": "user", "content": "\n\n".join(parts)}]

    def _parse_plan_json(self, text: str) -> dict:
        """从 Agent 输出中提取 JSON plan.

        待实现: 加入容错解析（Agent 可能在 JSON 前后输出额外文本）
        """
        # 简单实现：找到第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("Agent 未输出有效的 JSON plan")
        return json.loads(text[start:end])
