# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""EVALUATE 阶段处理器.

职责分三步，对齐官方 skill-creator 的评测流程：

1. Grader 评分 — 对每个 eval run 的 transcript + outputs 逐 assertion 评分
   输出 grading.json（expectations[].text/passed/evidence 格式）

2. Benchmark 聚合 — 遍历所有 grading.json，计算 per-config 的 mean/stddev/min/max
   输出 benchmark.json（前端根据此数据渲染 Benchmark 面板）

3. Analyst 分析 — 发现 aggregate stats 隐藏的模式
   输出 notes 列表（前端展示为分析摘要）

最终推送 EVAL_READY 事件 → 进入 REVIEW 挂起点（前端展示评测结果供用户审阅）。
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import (
    Benchmark,
    BenchmarkRun,
    GradingExpectation,
    GradingResult,
    MetricStats,
    SkillDevEventType,
    SkillDevStage,
    _now_iso,
)
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grader Agent 系统 Prompt
# 核心规则吸收自官方 agents/grader.md，不是外部文件引用
# ---------------------------------------------------------------------------

GRADER_SYSTEM_PROMPT = """\
你是一个评测 Grader。读取执行 transcript 和 output 文件，评估每条 expectation 是否通过。

## 评分标准

**PASS**：
- transcript / outputs 中有明确证据证明 expectation 为真
- 证据反映的是真实完成，不是表面合规（文件存在 ≠ 内容正确）

**FAIL**：
- 找不到证据，或证据与 expectation 矛盾
- 证据是表面的（文件名对但内容错/空）
- 无法从可用信息中验证

**不确定时**：按 FAIL 处理（举证责任在 expectation 一方）。

## 输出要求

对每条 expectation 输出：
- text: expectation 原文
- passed: true/false
- evidence: 引用的具体文本/描述

还需输出：
- summary: {{passed, failed, total, pass_rate}}
"""

# ---------------------------------------------------------------------------
# Analyst Agent 系统 Prompt
# 核心规则吸收自官方 agents/analyzer.md，不是外部文件引用
# ---------------------------------------------------------------------------

ANALYST_SYSTEM_PROMPT = """\
你是一个 Benchmark 分析师。分析所有评测运行结果，发现 aggregate 统计隐藏的模式。

关注维度：
- 某 expectation 在 with_skill 和 baseline 都 100% pass → 不具区分力
- 某 expectation 在两者都 fail → 超出能力或 expectation 本身有问题
- 某 eval 高方差 → 可能是 flaky 测试
- with_skill 反而劣于 baseline 的指标 → skill 可能在某方面产生负面影响
- 时间/token 开销 vs 通过率的权衡

输出一个 JSON 字符串数组，每条是一句简洁的观察（用中文）：
["观察1", "观察2", ...]
"""


class EvaluateStageHandler(StageHandler):
    """EVALUATE 阶段：Grader 评分 → Benchmark 聚合 → Analyst 分析."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        iteration = ctx.state.iteration
        iter_dir = ctx.workspace / "evals" / f"iteration-{iteration}"

        # --- Step 1: Grader 评分 ---
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在对测试结果进行评分..."}
        )
        await self._grade_all_evals(ctx, iter_dir)

        # --- Step 2: Benchmark 聚合 ---
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在聚合 benchmark 统计..."}
        )
        benchmark = self._aggregate_benchmark(ctx, iter_dir)

        # --- Step 3: Analyst 分析 ---
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "正在分析评测模式..."})
        analyst_notes = await self._analyze_patterns(ctx, benchmark)
        benchmark.notes = analyst_notes

        # 持久化
        benchmark_dict = benchmark.to_dict()
        (iter_dir / "benchmark.json").write_text(
            json.dumps(benchmark_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_md = self._render_benchmark_md(benchmark)
        (iter_dir / "benchmark.md").write_text(report_md, encoding="utf-8")

        ctx.state.eval_results = {"benchmark": benchmark_dict, "report": report_md}

        # 推送给前端 — 前端根据 benchmark JSON 渲染评测面板
        await ctx.emit(
            SkillDevEventType.EVAL_READY,
            {
                "benchmark": benchmark_dict,
                "iteration": iteration,
            },
        )
        return StageResult(next_stage=SkillDevStage.REVIEW)

    # ------------------------------------------------------------------
    # Step 1: Grader
    # ------------------------------------------------------------------

    async def _grade_all_evals(self, ctx: SkillDevContext, iter_dir: Path) -> None:
        """为每个 eval 的 with_skill / baseline 结果执行评分.

        待实现: 接入 create_stage_agent，用 GRADER_SYSTEM_PROMPT 调用 Agent
              逐 run 评分，把 transcript + outputs 作为上下文输入。
        """
        evals = (ctx.state.evals or {}).get("evals", [])
        for case in evals:
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            case_dir = iter_dir / eval_name
            expectations = case.get("expectations", [])

            for config in ("with_skill", "baseline"):
                run_dir = case_dir / config
                if not run_dir.exists():
                    continue

                # 待实现: 实际调用 Agent 评分
                # agent = ctx.create_stage_agent("grader", GRADER_SYSTEM_PROMPT, ...)
                # transcript = (run_dir / "transcript.md").read_text(...)
                # grading = await agent.grade(expectations, transcript, run_dir / "outputs")

                grading = GradingResult(
                    expectations=[
                        GradingExpectation(
                            text=exp, passed=False, evidence="待 Agent 实现"
                        )
                        for exp in expectations
                    ],
                    pass_rate=0.0,
                    passed_count=0,
                    failed_count=len(expectations),
                )
                (run_dir / "grading.json").write_text(
                    json.dumps(grading.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    # ------------------------------------------------------------------
    # Step 2: Benchmark 聚合
    # 逻辑内化自官方 aggregate_benchmark.py
    # ------------------------------------------------------------------

    def _aggregate_benchmark(self, ctx: SkillDevContext, iter_dir: Path) -> Benchmark:
        """遍历所有 grading.json + timing.json，聚合为 Benchmark."""
        evals = (ctx.state.evals or {}).get("evals", [])
        skill_name = (ctx.state.plan or {}).get("skill_name", "")

        configs: dict[str, list[BenchmarkRun]] = {}

        for case in evals:
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            case_dir = iter_dir / eval_name
            if not case_dir.exists():
                continue

            for config_dir in sorted(case_dir.iterdir()):
                if not config_dir.is_dir():
                    continue
                config = config_dir.name

                grading_file = config_dir / "grading.json"
                timing_file = config_dir / "timing.json"
                if not grading_file.exists():
                    continue

                grading = json.loads(grading_file.read_text(encoding="utf-8"))
                timing = (
                    json.loads(timing_file.read_text(encoding="utf-8"))
                    if timing_file.exists()
                    else {}
                )

                run = BenchmarkRun(
                    eval_id=case.get("id", 0),
                    eval_name=eval_name,
                    configuration=config,
                    pass_rate=grading.get("summary", {}).get("pass_rate", 0.0),
                    time_seconds=timing.get("total_duration_seconds", 0.0),
                    tokens=timing.get("total_tokens", 0),
                    expectations=grading.get("expectations", []),
                )
                configs.setdefault(config, []).append(run)

        # 聚合统计
        run_summary: dict[str, Any] = {}
        for config, runs in configs.items():
            run_summary[config] = {
                "pass_rate": _calc_stats([r.pass_rate for r in runs]).to_dict(),
                "time_seconds": _calc_stats([r.time_seconds for r in runs]).to_dict(),
                "tokens": _calc_stats([float(r.tokens) for r in runs]).to_dict(),
            }

        # 计算 delta
        config_names = list(configs.keys())
        if len(config_names) >= 2:
            a, b = run_summary[config_names[0]], run_summary[config_names[1]]
            run_summary["delta"] = {
                "pass_rate": f"{a['pass_rate']['mean'] - b['pass_rate']['mean']:+.2f}",
                "time_seconds": f"{a['time_seconds']['mean'] - b['time_seconds']['mean']:+.1f}",
                "tokens": f"{a['tokens']['mean'] - b['tokens']['mean']:+.0f}",
            }

        all_runs = [run for runs in configs.values() for run in runs]
        return Benchmark(
            skill_name=skill_name,
            runs=all_runs,
            run_summary=run_summary,
            timestamp=_now_iso(),
        )

    # ------------------------------------------------------------------
    # Step 3: Analyst
    # ------------------------------------------------------------------

    async def _analyze_patterns(
        self, ctx: SkillDevContext, benchmark: Benchmark
    ) -> list[str]:
        """分析 benchmark 结果，发现隐藏模式.

        待实现: 接入 create_stage_agent，用 ANALYST_SYSTEM_PROMPT 调用 Agent
              把 benchmark JSON 作为上下文，输出 notes 列表。
        """
        # 待实现: 实际调用 Agent
        # agent = ctx.create_stage_agent("analyst", ANALYST_SYSTEM_PROMPT, ...)
        # notes = await agent.analyze(json.dumps(benchmark.to_dict()))
        # return json.loads(notes)

        logger.warning("[EvaluateStage] _analyze_patterns 待接入 Agent")
        return ["评测分析 Agent 尚未接入"]

    # ------------------------------------------------------------------
    # Markdown 报告（给人看，也存入 workspace）
    # ------------------------------------------------------------------

    @staticmethod
    def _render_benchmark_md(benchmark: Benchmark) -> str:
        """把 Benchmark 渲染为 Markdown 报告."""
        rs = benchmark.run_summary
        configs = [k for k in rs if k != "delta"]

        lines = [
            f"# Skill Benchmark: {benchmark.skill_name}",
            "",
            f"**Date**: {benchmark.timestamp}",
            "",
            "## Summary",
            "",
        ]

        if len(configs) >= 2:
            a_name, b_name = configs[0], configs[1]
            a, b = rs[a_name], rs[b_name]
            delta = rs.get("delta", {})
            lines.append(f"| Metric | {a_name} | {b_name} | Delta |")
            lines.append("|--------|---------|---------|-------|")
            lines.append(
                f"| Pass Rate | {a['pass_rate']['mean'] * 100:.0f}% ± {a['pass_rate']['stddev'] * 100:.0f}% "
                f"| {b['pass_rate']['mean'] * 100:.0f}% ± {b['pass_rate']['stddev'] * 100:.0f}% "
                f"| {delta.get('pass_rate', '—')} |"
            )

        if benchmark.notes:
            lines.extend(["", "## Analyst Notes", ""])
            for note in benchmark.notes:
                lines.append(f"- {note}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 统计工具（内化自官方 aggregate_benchmark.py 的 calculate_stats）
# ---------------------------------------------------------------------------


def _calc_stats(values: list[float]) -> MetricStats:
    if not values:
        return MetricStats()
    n = len(values)
    mean = sum(values) / n
    stddev = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    return MetricStats(
        mean=round(mean, 4),
        stddev=round(stddev, 4),
        min=round(min(values), 4),
        max=round(max(values), 4),
    )
