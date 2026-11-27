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
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.stages.test_run_stage_runner import SkillDevTestRunner

logger = logging.getLogger(__name__)


class TestRunStageHandler(StageHandler):
    """TEST_RUN 阶段：子 Agent 并行执行测试用例（with_skill vs baseline）."""

    def __init__(
        self,
        max_concurrent: int = 5,
        timeout_seconds: int = 300,
        max_retries: int = 2,
    ):
        """初始化处理器
        
        Args:
            max_concurrent: 最大并发任务数
            timeout_seconds: 单个任务超时时间（秒）
            max_retries: 失败重试次数
        """
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        """执行 TEST_RUN 阶段"""
        # 验证前置条件
        try:
            self._validate_prerequisites(ctx)
        except ValueError as e:
            msg = f"TEST_RUN 前置条件验证失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_run"})
            raise RuntimeError(msg) from e

        eval_cases = ctx.state.evals.get("evals", [])
        iteration = ctx.state.iteration
        iter_dir = ctx.workspace / "evals" / f"iteration-{iteration}"
        try:
            iter_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            msg = f"TEST_RUN 无法创建迭代目录 {iter_dir}：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_run"})
            raise RuntimeError(msg) from e

        total_tasks = len(eval_cases) * 2  # with_skill + baseline
        await ctx.emit(
            SkillDevEventType.TEST_PROGRESS,
            {
                "total": total_tasks,
                "completed": 0,
                "message": f"开始执行 {len(eval_cases)} 个测试用例 (iteration {iteration})...",
            },
        )

        try:
            results = await self._run_all_evals(ctx, eval_cases, iter_dir)
        except Exception as e:
            msg = f"TEST_RUN 测试执行失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_run"})
            raise RuntimeError(msg) from e

        summary = self._generate_summary(
            results,
            len(eval_cases),
            session_id=ctx.state.task_id,
        )
        summary_file = iter_dir / "iteration_summary.json"
        try:
            summary_file.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except OSError as e:
            msg = f"TEST_RUN 汇总文件写入失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_run"})
            raise RuntimeError(msg) from e

        await ctx.emit(SkillDevEventType.TEST_PROGRESS, {
            "total": total_tasks,
            "completed": total_tasks,
            "message": (
                f"测试完成！成功: {summary['successful']}, "
                f"失败: {summary['failed']}, "
                f"成功率: {summary['success_rate']:.1f}%"
            )
        })

        return StageResult(next_stage=SkillDevStage.EVALUATE)

    def _validate_prerequisites(self, ctx: SkillDevContext) -> None:
        """验证前置条件
        
        Args:
            ctx: SkillDevContext 实例
            
        Raises:
            ValueError: 如果前置条件不满足
        """
        # 检查 evals 存在
        if not ctx.state.evals or not ctx.state.evals.get("evals"):
            raise ValueError(
                "TEST_RUN 阶段缺少测试用例，请先完成 TEST_DESIGN 阶段"
            )

        # 检查有效的评估用例
        eval_cases = ctx.state.evals.get("evals", [])
        if not isinstance(eval_cases, list) or len(eval_cases) == 0:
            raise ValueError("评估用例列表为空或格式不正确")

        # 验证每个 eval case 都有 name 字段
        for idx, case in enumerate(eval_cases):
            if not isinstance(case, dict):
                raise ValueError(f"eval case #{idx} 不是 dict")
            if "name" not in case or not case["name"]:
                raise ValueError(
                    f"eval case #{idx} 缺少 'name' 字段，请先运行 TEST_DESIGN 阶段"
                )

        # 检查 skill 目录（可选，baseline 仍可运行）
        skill_dir = ctx.workspace / "skill"
        if not skill_dir.exists():
            logger.warning(
                "[session=%s] [TestRunStage] Skill 目录不存在: %s。将仅执行 baseline 测试。",
                ctx.state.task_id,
                skill_dir,
            )

    async def _run_all_evals(self, ctx: SkillDevContext, eval_cases: list[dict], iter_dir) -> list[dict]:
        """并行执行所有测试用例.
        
        为每个 eval case 创建两个并行任务：
        - with_skill: 注入 skill 后执行
        - baseline: 不注入 skill 执行
        """
        # 创建测试运行器（并行执行时关闭 token 流推送，避免多 agent 日志混杂）
        runner = SkillDevTestRunner(
            ctx=ctx,
            skill_dir=ctx.workspace / "skill",
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            emit_thinking=False,
        )

        # 使用 Semaphore 限制并发（防止资源耗尽）
        semaphore = asyncio.Semaphore(self.max_concurrent)

        # 创建所有任务
        tasks = []

        for case in eval_cases:
            case_id = case.get("id", 0)
            case_name = case.get("name", f"eval-{case_id}")
            case_dir = iter_dir / case_name
            case_dir.mkdir(parents=True, exist_ok=True)

            # 保存 eval 元数据
            metadata = {
                "eval_id": case_id,
                "eval_name": case_name,
                "case_category": case.get("case_category", ""),
                "prompt": case.get("prompt", ""),
                "expected_output": case.get("expected_output", ""),
                "expectations": case.get("expectations", [])
            }
            (case_dir / "eval_metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            # 为每个配置创建任务（with_skill 和 baseline）
            for variant in ["with_skill", "baseline"]:
                run_func = runner.run_with_skill if variant == "with_skill" else runner.run_baseline
                task = self._bounded_task(
                    semaphore, ctx, case_name, variant,
                    run_func, case, case_dir,
                )
                tasks.append((case_name, variant, case_id, task))

        # 执行任务并收集结果
        total_tasks = len(tasks)
        # 用线程安全的计数器统计已完成数（gather 并行，但 emit 是协程，串行安全）
        completed_count = 0

        async def _bounded_and_emit(case_name, variant, case_id, coro):
            nonlocal completed_count
            processed = await coro  # 等待单个任务完成（由 _bounded_task 内部限速）

            # 任务一结束立即 emit，不等其他任务
            processed_result = self._process_and_validate_runner_result(
                processed,
                case_name,
                variant,
                case_id,
                session_id=ctx.state.task_id,
            )
            self._ensure_result_files_created(
                iter_dir / case_name / variant,
                processed_result,
                session_id=ctx.state.task_id,
            )
            completed_count += 1
            await ctx.emit(
                SkillDevEventType.TEST_PROGRESS,
                {
                    "completed": completed_count,
                    "total": total_tasks,
                    "case_name": case_name,
                    "variant": variant,
                    "case_status": processed_result.get("status", "error"),
                    "message": (
                        f"已完成: {case_name}/{variant} "
                        f"({'成功' if processed_result.get('status') == 'success' else '失败'})，"
                        f"进度：{completed_count}/{total_tasks}"
                    ),
                },
            )
            return processed_result

        wrapped = [
            _bounded_and_emit(case_name, variant, case_id, task)
            for case_name, variant, case_id, task in tasks
        ]
        results = await asyncio.gather(*wrapped, return_exceptions=True)
        # 过滤掉异常（_bounded_task 已处理，不应出现，但防御性保留）
        results = [r for r in results if isinstance(r, dict)]

        logger.info("[session=%s] [TestRunStage] 所有任务完成，共 %d 个结果", ctx.state.task_id, len(results))
        return results

    async def _bounded_task(self, semaphore, ctx, case_name: str, variant: str, func, *args):
        """限制并发的任务执行，在获得信号量（实际开始执行）时推送进度事件."""
        # args[0] 是 case dict，从中取 prompt 附加到 start 信号，供前端在卡片顶部展示
        prompt = args[0].get("prompt", "") if args and isinstance(args[0], dict) else ""
        async with semaphore:
            await ctx.emit(SkillDevEventType.TEST_PROGRESS, {
                "message": f"开始执行: {case_name}/{variant}",
                "case_name": case_name,
                "variant": variant,
                "prompt": prompt,
            })
            return await func(*args)

    def _process_and_validate_runner_result(
        self,
        result: Any,
        case_name: str,
        variant: str,
        case_id: int,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """处理和验证 SkillDevTestRunner 的返回值.
        
        Args:
            result: runner.run_*() 的原始返回值
            case_name: eval case 名称
            variant: 变量名 (with_skill|baseline)
            case_id: eval ID
            
        Returns:
            规范化的结果 dict
        """
        # 情况 1：异常返回
        if isinstance(result, Exception):
            logger.error(
                "[session=%s] [TestRunStage] %s/%s 执行异常: %s",
                session_id,
                case_name,
                variant,
                result,
                exc_info=result
            )
            return {
                "status": "error",
                "error_message": str(result),
                "error_type": type(result).__name__,
                "case_name": case_name,
                "variant": variant,
                "eval_id": case_id,
                "output": "",
                "execution_time_seconds": 0,
                "files_created": [],
            }

        # 情况 2：没有返回值或非 dict
        if not isinstance(result, dict):
            logger.warning(
                "[session=%s] [TestRunStage] %s/%s 返回值不是 dict: %s",
                session_id,
                case_name,
                variant,
                type(result),
            )
            return {
                "status": "error",
                "error_message": f"返回值类型错误: {type(result).__name__}",
                "case_name": case_name,
                "variant": variant,
                "eval_id": case_id,
                "output": str(result)[:500],  # 截断长输出
                "execution_time_seconds": 0,
                "files_created": [],
            }

        # 情况 3：正常返回，验证 schema
        validated = {
            "status": result.get("status", "unknown"),
            "case_name": case_name,
            "variant": variant,
            "eval_id": case_id,
            "output": result.get("output", ""),
            "execution_time_seconds": result.get("duration_seconds", 0),
            "tokens_used": result.get("tokens_used"),
            "files_created": result.get("files_created", []),
        }

        # 如果 status 不在预期的值中，标记为 error
        if validated["status"] not in ("success", "error", "timeout"):
            logger.warning(
                "[session=%s] [TestRunStage] %s/%s status 非预期值: %s",
                session_id,
                case_name,
                variant,
                validated["status"],
            )
            validated["status"] = "error"
            validated["error_message"] = f"未知的 status: {result.get('status')}"

        # 如果是 error 或 timeout，确保有 error_message
        if validated["status"] != "success" and "error_message" not in validated:
            validated["error_message"] = result.get("error_message", "未知错误")

        return validated

    def _ensure_result_files_created(
        self,
        variant_dir: Path,
        result: Dict[str, Any],
        session_id: str = "",
    ) -> None:
        """确保 result.json 和 timing.json 被创建.
        
        如果 SkillDevTestRunner 由于异常状态没有创建这些文件，则由本方法创建。
        
        Args:
            variant_dir: {case_dir}/{variant}/ 目录路径
            result: 规范化后的执行结果
        """
        variant_dir.mkdir(parents=True, exist_ok=True)

        # 创建 result.json
        result_file = variant_dir / "result.json"
        if not result_file.exists():
            result_data = {
                "status": result.get("status", "unknown"),
                "output": result.get("output", ""),
                "case_name": result.get("case_name", ""),
                "variant": result.get("variant", ""),
                "eval_id": result.get("eval_id", 0),
                "files_created": result.get("files_created", []),
            }
            if result.get("error_message"):
                result_data["error_message"] = result["error_message"]

            result_file.write_text(
                json.dumps(result_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info("[session=%s] [TestRunStage] 创建: %s", session_id, result_file)

        # 创建 timing.json
        timing_file = variant_dir / "timing.json"
        if not timing_file.exists():
            timing_data = {
                "total_duration_seconds": result.get("execution_time_seconds", 0),
                "total_tokens": result.get("tokens_used", 0),
                "timestamp": json.dumps({"now": time.time()}),
            }
            timing_file.write_text(
                json.dumps(timing_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info("[session=%s] [TestRunStage] 创建: %s", session_id, timing_file)

    def _generate_summary(
        self, results: List[Dict], eval_count: int, session_id: str = ""
    ) -> Dict[str, Any]:
        """生成汇总报告
        
        Args:
            results: 执行结果列表
            eval_count: eval case 总数
            
        Returns:
            汇总统计 dict
        """
        if not results:
            return {
                "total_evals": eval_count,
                "total_tasks": 0,
                "successful": 0,
                "failed": 0,
                "success_rate": 0.0,
            }

        # 使用规范化的 status 字段
        successful = sum(1 for r in results if r.get("status") == "success")
        failed = len(results) - successful

        summary = {
            "total_evals": eval_count,
            "total_tasks": len(results),
            "successful": successful,
            "failed": failed,
            "success_rate": successful / len(results) * 100 if results else 0.0,
        }

        logger.info(
            "[session=%s] [TestRunStage] 汇总: 成功 %d/%d, 失败率 %.1f%%",
            session_id,
            successful,
            len(results),
            summary["success_rate"],
        )

        return summary
