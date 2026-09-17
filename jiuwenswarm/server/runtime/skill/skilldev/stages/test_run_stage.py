# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TEST_RUN 阶段处理器.

职责：
- 为每个测试用例并行创建两个子 Agent：
    · with_skill：注入当前生成的 Skill 后执行用例
    · baseline：不注入 Skill，作为对照组
- 收集两组结果，写入 iteration-{N}/ 目录
- 推送 TEST_PROGRESS 事件反馈进度

这是整个 Pipeline 中技术复杂度最高的阶段，涉及：
- 子 Agent 创建与 Skill 注入
- 并行任务调度与结果收集
- 文件系统隔离（每个测试用例独立目录）

扩展点：
- 子 Agent 并行度可配置（默认与测试用例数相同）
- with_skill / baseline 的执行逻辑封装在 SkillDevTestRunner 中（待独立模块实现）
"""

from __future__ import annotations

import asyncio
import json
import logging

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)


class TestRunStageHandler(StageHandler):
    """TEST_RUN 阶段：子 Agent 并行执行测试用例（with_skill vs baseline）."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        evals = ctx.state.evals
        if not evals or not evals.get("evals"):
            raise ValueError("TEST_RUN 阶段缺少测试用例，请先完成 TEST_DESIGN 阶段")

        eval_cases = evals["evals"]
        iteration = ctx.state.iteration
        iter_dir = ctx.workspace / "evals" / f"iteration-{iteration}"
        iter_dir.mkdir(parents=True, exist_ok=True)

        total_tasks = len(eval_cases) * 2  # with_skill + baseline
        await ctx.emit(
            SkillDevEventType.TEST_PROGRESS,
            {
                "total": total_tasks,
                "completed": 0,
                "message": f"开始执行 {len(eval_cases)} 个测试用例...",
            },
        )

        results = await self._run_all_evals(ctx, eval_cases, iter_dir)

        await ctx.emit(
            SkillDevEventType.TEST_PROGRESS,
            {
                "total": total_tasks,
                "completed": total_tasks,
                "message": "测试执行完成",
            },
        )

        return StageResult(next_stage=SkillDevStage.EVALUATE)

    async def _run_all_evals(
        self, ctx: SkillDevContext, eval_cases: list[dict], iter_dir
    ) -> list[dict]:
        """并行执行所有测试用例.

        待实现: 接入 SkillDevTestRunner，为每个用例创建 with_skill + baseline 子 Agent
        """
        # 待实现:
        # tasks = []
        # for case in eval_cases:
        #     case_dir = iter_dir / case["name"]
        #     case_dir.mkdir(parents=True, exist_ok=True)
        #     tasks.append(self._run_single_eval(ctx, case, case_dir))
        # results = await asyncio.gather(*tasks, return_exceptions=True)
        # return results

        logger.warning("[TestRunStage] _run_all_evals 尚未实现，写入占位结果")
        results = []
        for case in eval_cases:
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            case_dir = iter_dir / eval_name
            (case_dir / "with_skill").mkdir(parents=True, exist_ok=True)
            (case_dir / "baseline").mkdir(parents=True, exist_ok=True)

            # 写入 eval_metadata.json（对齐官方格式）
            eval_metadata = {
                "eval_id": case.get("id", 0),
                "eval_name": eval_name,
                "prompt": case.get("prompt", ""),
                "assertions": case.get("assertions", []),
            }
            (case_dir / "eval_metadata.json").write_text(
                json.dumps(eval_metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # 占位 timing.json（实际应从 subagent task notification 中捕获）
            timing_placeholder = {
                "total_tokens": 0,
                "duration_ms": 0,
                "total_duration_seconds": 0.0,
            }
            for config in ("with_skill", "baseline"):
                config_dir = case_dir / config
                (config_dir / "result.json").write_text(
                    '{"status": "待实现", "output": "待实现"}', encoding="utf-8"
                )
                (config_dir / "timing.json").write_text(
                    json.dumps(timing_placeholder, indent=2), encoding="utf-8"
                )

            results.append({"eval_id": case.get("id", 0), "status": "placeholder"})
            await ctx.emit(
                SkillDevEventType.TEST_PROGRESS,
                {
                    "message": f"已完成（占位）：{eval_name}",
                },
            )
        return results

    async def _run_single_eval(
        self, ctx: SkillDevContext, case: dict, case_dir
    ) -> dict:
        """为单个测试用例创建 with_skill + baseline 两组子 Agent 并行执行.

        待实现: 实现 SkillDevTestRunner.run(case, skill_dir, case_dir)
        """
        # with_skill_result, baseline_result = await asyncio.gather(
        #     self._run_with_skill(ctx, case, case_dir / "with_skill"),
        #     self._run_baseline(ctx, case, case_dir / "baseline"),
        # )
        # return {"eval_id": case["id"], "with_skill": with_skill_result, "baseline": baseline_result}
        raise NotImplementedError
