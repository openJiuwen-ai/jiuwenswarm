# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""DESC_OPTIMIZE 阶段处理器.

核心流程（对齐官方 Description Optimization，但用我们自己的模型 API 实现）：

1. Agent 生成 ~20 个 trigger eval queries（should_trigger / should_not_trigger）
2. Train/test split (60% / 40%)
3. 迭代优化循环（最多 max_iterations 轮）：
   a. 对每个 query，调用模型判断当前 description 是否会触发
   b. 统计 pass rate
   c. 基于失败案例，调用模型生成改进的 description
   d. 如果 train 全部通过则提前退出
4. 选 test score 最高的 description（防过拟合）
5. 将 best_description 写回 SKILL.md frontmatter

官方实现用 `claude -p` CLI subprocess 做触发测试和描述改进。
我们的实现通过 ctx.create_stage_agent 直接调用模型 API，不依赖 CLI。
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import (
    DescOptimizeIteration,
    SKILL_DESC_MAX_LEN,
    SkillDevEventType,
    SkillDevStage,
    TriggerEvalQuery,
)
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult
from jiuwenswarm.server.runtime.skill.skilldev.stages.validate_stage import (
    parse_skill_frontmatter,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5
HOLDOUT_RATIO = 0.4

# ---------------------------------------------------------------------------
# Prompts（内化自官方 improve_description.py 的 prompt 结构）
# ---------------------------------------------------------------------------

TRIGGER_QUERY_GEN_PROMPT = """\
你是一个 Skill 触发优化专家。根据以下 Skill 的名称和描述，生成 20 个测试查询。

Skill 名称: {skill_name}
当前 Description: {description}

## 要求

### should_trigger=true 的查询（约 10 个）
- 用户确实需要这个 Skill 时会说的话
- 不同表达风格（正式/随意/简短/详细）
- 有些不直接提及 Skill 名称但确实需要其功能
- 包含具体细节（文件路径、个人背景、数据名称等）

### should_trigger=false 的查询（约 10 个）
- 关键词相近但实际不需要这个 Skill 的 **近似场景**
- 相邻领域、歧义措辞、看似相关但应由其他工具处理
- 不要用明显无关的查询（"写斐波那契函数"对 PDF 技能来说太容易区分了）

输出 JSON 数组：
[{{"query": "具体的用户查询", "should_trigger": true}}, ...]
"""

IMPROVE_DESC_PROMPT = """\
你正在优化一个名为 "{skill_name}" 的 Skill 的 description 字段。
description 出现在模型的 available_skills 列表中，模型仅凭 description 决定是否使用该 Skill。

当前 description：
"{current_description}"

当前得分：{scores_summary}

{failure_details}

{history_section}

## 要求

根据失败案例，写一个更好的 description：
- 从失败中 **泛化**，不要过拟合到具体查询
- 用祈使句（"Use when..." 而非 "This skill does..."）
- 聚焦用户意图而非实现细节
- 让触发场景具体且可区分
- 严格不超过 {max_len} 字符

请在 <new_description> 标签中只输出新的 description 文本：
<new_description>新描述内容</new_description>
"""


@dataclass
class _OptimizationLoopInput:
    """描述优化循环的输入参数封装."""

    skill_name: str
    skill_body: str
    current_desc: str
    train_set: list[TriggerEvalQuery]
    test_set: list[TriggerEvalQuery]


@dataclass
class _ImproveDescriptionInput:
    """描述改进步骤的输入参数封装."""

    skill_name: str
    skill_body: str
    current_desc: str
    train_results: list[dict]
    history: list[DescOptimizeIteration]


class DescOptimizeStageHandler(StageHandler):
    """DESC_OPTIMIZE 阶段：优化 SKILL.md 的 description 以提高触发准确率."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        skill_dir = ctx.workspace / "skill"
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            await ctx.emit(
                SkillDevEventType.PROGRESS, {"message": "未找到 SKILL.md，跳过描述优化"}
            )
            return StageResult(next_stage=SkillDevStage.COMPLETED)

        skill_name, current_desc, body = parse_skill_frontmatter(skill_md)

        # Step 1: 生成触发测试查询
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在生成触发测试查询集..."}
        )
        queries = await self._generate_trigger_queries(ctx, skill_name, current_desc)

        # Step 2: Train/test split
        train_set, test_set = self._split_eval_set(queries, HOLDOUT_RATIO)

        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {
                "message": f"开始描述优化循环（train={len(train_set)}, test={len(test_set)}）",
            },
        )

        # Step 3: 优化循环
        loop_input = _OptimizationLoopInput(
            skill_name=skill_name,
            skill_body=body,
            current_desc=current_desc,
            train_set=train_set,
            test_set=test_set,
        )
        best_desc, history = await self._optimization_loop(ctx, loop_input)

        # Step 4: 写回 SKILL.md
        if best_desc and best_desc != current_desc:
            self._apply_description(skill_md, current_desc, best_desc)

        # Step 5: 结果
        best_iter = (
            max(history, key=lambda h: h.test_passed or 0)
            if test_set and history
            else (max(history, key=lambda h: h.train_passed) if history else None)
        )
        result = {
            "original_description": current_desc,
            "best_description": best_desc,
            "best_score": f"{best_iter.test_passed}/{best_iter.test_total}"
            if best_iter and best_iter.test_passed is not None
            else (
                f"{best_iter.train_passed}/{best_iter.train_total}"
                if best_iter
                else "N/A"
            ),
            "iterations_run": len(history),
            "history": [h.to_dict() for h in history],
        }
        ctx.state.desc_optimize_result = result

        await ctx.emit(SkillDevEventType.DESC_OPT_READY, result)
        return StageResult(next_stage=SkillDevStage.COMPLETED)

    # ------------------------------------------------------------------
    # 生成触发测试查询
    # ------------------------------------------------------------------

    async def _generate_trigger_queries(
        self,
        ctx: SkillDevContext,
        skill_name: str,
        description: str,
    ) -> list[TriggerEvalQuery]:
        """调用 Agent 生成 ~20 个触发测试查询.

        待实现: 接入 create_stage_agent
        """
        # 待实现:
        # agent = ctx.create_stage_agent("desc_opt_gen", prompt, ...)
        # output = await agent.run(...)
        # parsed = json.loads(output)
        # return [TriggerEvalQuery(**q) for q in parsed]

        logger.warning("[DescOptimize] _generate_trigger_queries 待接入 Agent")
        return [
            TriggerEvalQuery(
                query=f"帮我用 {skill_name} 完成一个任务", should_trigger=True
            ),
            TriggerEvalQuery(query="帮我写一个排序算法", should_trigger=False),
        ]

    # ------------------------------------------------------------------
    # Train/test split（内化自官方 run_loop.py 的 split_eval_set）
    # ------------------------------------------------------------------

    @staticmethod
    def _split_eval_set(
        queries: list[TriggerEvalQuery],
        holdout: float,
        seed: int = 42,
    ) -> tuple[list[TriggerEvalQuery], list[TriggerEvalQuery]]:
        """按 should_trigger 分层切分 train/test."""
        rng = random.Random(seed)

        trigger = [q for q in queries if q.should_trigger]
        no_trigger = [q for q in queries if not q.should_trigger]
        rng.shuffle(trigger)
        rng.shuffle(no_trigger)

        n_t = max(1, int(len(trigger) * holdout))
        n_nt = max(1, int(len(no_trigger) * holdout))

        test = trigger[:n_t] + no_trigger[:n_nt]
        train = trigger[n_t:] + no_trigger[n_nt:]
        return train, test

    # ------------------------------------------------------------------
    # 优化循环（内化自官方 run_loop.py 的核心逻辑）
    # ------------------------------------------------------------------

    async def _optimization_loop(
        self,
        ctx: SkillDevContext,
        loop_input: _OptimizationLoopInput,
    ) -> tuple[str, list[DescOptimizeIteration]]:
        """运行 eval → improve 循环，返回 (best_description, history)."""
        skill_name = loop_input.skill_name
        skill_body = loop_input.skill_body
        current_desc = loop_input.current_desc
        train_set = loop_input.train_set
        test_set = loop_input.test_set
        history: list[DescOptimizeIteration] = []

        for i in range(1, MAX_ITERATIONS + 1):
            await ctx.emit(
                SkillDevEventType.PROGRESS,
                {
                    "message": f"描述优化第 {i}/{MAX_ITERATIONS} 轮...",
                },
            )

            # 评估 train + test
            train_results = await self._eval_description(ctx, current_desc, train_set)
            test_results = (
                await self._eval_description(ctx, current_desc, test_set)
                if test_set
                else None
            )

            train_passed = sum(1 for r in train_results if r["pass"])
            iteration = DescOptimizeIteration(
                iteration=i,
                description=current_desc,
                train_passed=train_passed,
                train_total=len(train_set),
                test_passed=sum(1 for r in test_results if r["pass"])
                if test_results
                else None,
                test_total=len(test_set) if test_results else None,
            )
            history.append(iteration)

            # 全部通过则提前退出
            if train_passed == len(train_set):
                break

            # 最后一轮不再改进
            if i == MAX_ITERATIONS:
                break

            # 改进 description
            improve_input = _ImproveDescriptionInput(
                skill_name=skill_name,
                skill_body=skill_body,
                current_desc=current_desc,
                train_results=train_results,
                history=history,
            )
            current_desc = await self._improve_description(ctx, improve_input)

        # 选 test score 最高的（防过拟合）
        if test_set:
            best = max(history, key=lambda h: h.test_passed or 0)
        else:
            best = max(history, key=lambda h: h.train_passed)
        return best.description, history

    # ------------------------------------------------------------------
    # 单次评估：判断 description 对一组 queries 是否触发
    # ------------------------------------------------------------------

    async def _eval_description(
        self,
        ctx: SkillDevContext,
        description: str,
        queries: list[TriggerEvalQuery],
    ) -> list[dict]:
        """对每个 query，调用模型判断当前 description 是否会触发.

        待实现: 接入 create_stage_agent 实际评估
              核心问题是模拟"模型看到 skill description 后是否会读取该 skill"
        """
        # 待实现:
        # for query in queries:
        #     triggered = await self._test_single_trigger(ctx, description, query.query)
        #     ...

        logger.warning("[DescOptimize] _eval_description 待接入 Agent")
        return [
            {
                "query": q.query,
                "should_trigger": q.should_trigger,
                "triggered": q.should_trigger,  # 占位：假设全部正确
                "pass": True,
            }
            for q in queries
        ]

    # ------------------------------------------------------------------
    # 改进 description（内化自官方 improve_description.py 的 prompt 结构）
    # ------------------------------------------------------------------

    async def _improve_description(
        self,
        ctx: SkillDevContext,
        improve_input: _ImproveDescriptionInput,
    ) -> str:
        """调用模型基于失败案例改进 description.

        待实现: 接入 create_stage_agent
        """
        # 待实现:
        # failed_triggers = [r for r in train_results if r["should_trigger"] and not r["pass"]]
        # false_triggers = [r for r in train_results if not r["should_trigger"] and not r["pass"]]
        # prompt = IMPROVE_DESC_PROMPT.format(...)
        # agent = ctx.create_stage_agent("desc_improver", prompt, ...)
        # output = await agent.run(...)
        # return _extract_new_description(output)

        logger.warning("[DescOptimize] _improve_description 待接入 Agent")
        return improve_input.current_desc

    # ------------------------------------------------------------------
    # 将优化后的 description 写回 SKILL.md
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_description(skill_md: Path, old_desc: str, new_desc: str) -> None:
        """替换 SKILL.md frontmatter 中的 description 字段."""
        content = skill_md.read_text(encoding="utf-8")

        match = re.match(r"^(---\n)(.*?)(\n---)", content, re.DOTALL)
        if not match:
            return

        frontmatter = match.group(2)
        # 替换 description 行（简单场景：单行 description: xxx）
        new_fm = re.sub(
            r"(description:\s*).*",
            rf"\g<1>{new_desc}",
            frontmatter,
            count=1,
        )
        new_content = match.group(1) + new_fm + match.group(3) + content[match.end():]
        skill_md.write_text(new_content, encoding="utf-8")
