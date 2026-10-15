# -*- coding: utf-8 -*-
"""
check_feasibility_and_downgrade.py — 第 3 阶段：门禁校验 + 算力档位判定

按 [workflows/planning.md](../workflows/planning.md) Phase C 实现。
校验 experiment_matrix / data_plan 结构性 + 算力档位判定。

注：**降级策略不在此处自动执行**——超预算时（tier > 0）由调用方（planning_flow.py）
决定是走 `downgrade_with_llm_async`（LLM 智能降级）还是人工介入——见
[references/method-designer.md §Self-Reflection Checklist](../references/method-designer.md) 与
[planning-schemas.md §6 三档降级](../references/planning-schemas.md)。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from scripts.estimate_compute import judge_tier
from scripts.plan_experiment_with_data import (
    _normalize_compute_estimate,
    _normalize_planner_output,
)

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.check_feasibility")

# 算力档位（见 [planning-schemas.md §4](../references/planning-schemas.md)）
TIER_FULL = 0      # 精算
TIER_TRIM = 1      # 消减
TIER_CORE = 2      # 核心优先

# ExecutionConfig 默认值（schemas §2.5；用户未指定时兜底）
_DEFAULT_SEEDS: list[int] = [42]
_DEFAULT_MAX_RETRIES: int = 2
_DEFAULT_TIMEOUT_SECONDS: int | None = None
_DEFAULT_DRY_RUN: bool = False


def check_feasibility_and_downgrade(
    experiment_plan: dict,
    data_plan: dict,
    resource_constraints: dict,
) -> tuple[dict, dict, int, dict]:
    """
    门禁校验 + 算力档位判定（**不自动降级**——降级由调用方决定）。

    步骤:
        1. 校验 experiment_matrix 结构完整性
        2. 校验 data_plan 业务真实性（4 项）
        3. 算力估算 + judge_tier 档位判定
        4. 返回 (ep, dp, tier, validation_results)

    注：超预算时（tier > 0）本函数不修改 ep/dp；调用方（main.py）应：
        - 提示用户当前 tier + 建议
        - 由用户选择：接受 LLM 自动降级 / 手动改 budget / abort
        - 选 LLM 降级时调用本模块的 `downgrade_with_llm()`

    Args:
        experiment_plan:      第 2 阶段产出的实验方案 dict。
        data_plan:            第 2 阶段产出的数据订单 dict。
        resource_constraints: inputs["resource_constraints"]。

    Returns:
        (experiment_plan, data_plan, tier, validation_results):
            experiment_plan:     不修改，原样返回。
            data_plan:           不修改，原样返回。
            tier:                算力档位（0/1/2）。
            validation_results:  校验结果汇总:
                {
                    "experiment_matrix":   "pass" | "fail",
                    "data_plan":           "pass" | "fail",
                    "compute_tier":        0 | 1 | 2,
                }
    """
    validation_results: dict = {}

    # 1. 校验 experiment_matrix（仅结构性）
    validation_results["experiment_matrix"] = (
        "pass" if _validate_experiment_matrix(experiment_plan) else "fail"
    )

    # 2. 校验 data_plan（业务真实性：4 项结构性）
    validation_results["data_plan"] = (
        "pass" if _check_data_plan_realism(data_plan) else "fail"
    )

    # 3-4. 算力估算 + 档位判定
    budget = resource_constraints.get("budget")
    total_cost = float(experiment_plan.get("compute_estimate", 0.0))
    tier, over_ratio = judge_tier(total_cost, budget)
    validation_results["compute_tier"] = tier

    if tier > TIER_FULL:
        log.warning(
            f"[check_feasibility] 算力超预算: total={total_cost}, budget={budget}, "
            f"over_ratio={'+inf' if over_ratio == float('inf') else f'{over_ratio:.1%}'}, "
            f"tier={tier}（TIER_TRIM=1 / TIER_CORE=2）→ 调用方决定如何处理"
        )

    return experiment_plan, data_plan, tier, validation_results


async def downgrade_with_llm_async(
    experiment_plan: dict,
    method_design: dict,
    method_review: dict,
    resource_constraints: dict,
    data_plan: dict | None = None,
) -> tuple[dict, dict]:
    """
    LLM 驱动的降级：调用 experiment-planner 让 LLM 按"对核心创新点的支撑度"裁剪 plan（异步）。

    不再用"按出现顺序截断"启发式——哪些 ablation / 哪些主实验该留，LLM 自己判断。

    Args:
        experiment_plan:       当前实验方案（可能超预算）。
        method_design:         阶段 1 产出的方法设计（LLM 用来判断哪些创新点最关键）。
        method_review:         阶段 1 产出的评审。
        resource_constraints:  资源约束；`budget` 字段必填（None 时 LLM 没法判断）。
        data_plan:             当前数据订单（可选；LLM 不会改它，但会知道有哪些 dataset 可用）。

    Returns:
        (new_experiment_plan, new_data_plan):
            new_experiment_plan: 降级后的实验方案。
            new_data_plan:       重新按降级后的 plan 抽取的 data_order。
    """
    from scripts._subagent import call_agent_async
    from scripts.plan_experiment_with_data import _extract_data_order

    budget = resource_constraints.get("budget")
    if budget is None:
        raise ValueError(
            "downgrade_with_llm 需要 budget；budget=None 时 judge_tier 返回 TIER_FULL，"
            "本函数不该被调用"
        )

    payload: dict = {
        "method_design": method_design,
        "method_review": method_review,
        "resource_constraints": resource_constraints,
        "current_experiment_plan": experiment_plan,
        "current_data_plan": data_plan or {},
        "mode": "downgrade",
        "budget_constraint": budget,
        "instruction": (
            f"你的实验方案当前估算 {experiment_plan.get('compute_estimate', 0.0)} GPU-hours，"
            f"用户预算 {budget} GPU-hours。请按以下优先级裁剪，生成**新的** experiment_plan：\n"
            "1. 保留所有 `primary_experiments` 中标为「对应核心创新点」的实验\n"
            "2. ablation_plan 按「对核心创新点的支撑度」从高到低排序，砍掉排序靠后的项\n"
            "3. baselines 至少保留 1 个最弱基线 + 1 个最强基线（用于对比）\n"
            "4. metrics 全部保留（廉价）\n"
            "5. compute_estimate 重新计算，必须 ≤ budget\n"
            "6. success_criteria 至少保留 1 条最关键的\n"
            "请用 Self-Reflection Checklist 验证：每条保留的实验是否能回答核心创新点？"
        ),
    }

    log.info(
        f"[check_feasibility] LLM 驱动降级: total={experiment_plan.get('compute_estimate', 0.0)} → "
        f"target budget={budget}"
    )
    combined = await call_agent_async(
        "experiment-planner", payload, phase="门禁校验",
    )
    # 兼容 deepseek-v4-flash 偶尔漏外层包装——自动包一层。
    combined = _normalize_planner_output(combined)
    if "experiment_plan" not in combined:
        raise RuntimeError(
            f"experiment-planner(downgrade) 返回缺少 'experiment_plan' 键，"
            f"实际 keys={list(combined.keys())}"
        )
    new_ep = combined["experiment_plan"]
    # 归一化 compute_estimate：抽嵌套 dict 里的 total_gpu_hours，schema 期望 number
    _normalize_compute_estimate(new_ep)

    # 重新抽 data_plan（dataset 列表可能变了）—— 走确定性派生，不用 LLM 那个
    new_dp = _extract_data_order(new_ep)

    # 验证：compute_estimate 必须 ≤ budget
    new_total = float(new_ep.get("compute_estimate", 0.0))
    if new_total > budget:
        log.warning(
            f"[check_feasibility][WARN] LLM 降级后仍超预算: {new_total} > {budget}；"
            f"用户可能需要再调 budget 或接受超支"
        )

    return new_ep, new_dp


def downgrade_with_llm(
    experiment_plan: dict,
    method_design: dict,
    method_review: dict,
    resource_constraints: dict,
    data_plan: dict | None = None,
) -> tuple[dict, dict]:
    """同步包装：``asyncio.run(downgrade_with_llm_async(...))``。

    workflow 调起路径请直接用 ``await downgrade_with_llm_async(...)``。
    """
    return asyncio.run(downgrade_with_llm_async(
        experiment_plan, method_design, method_review, resource_constraints, data_plan,
    ))


def _validate_experiment_matrix(experiment_plan: dict) -> bool:
    """
    校验 experiment_matrix 结构完整性（仅结构性，不做内容判断）:
        - baselines 非空
        - metrics 非空
        - experiment_matrix 至少 1 行
        - experiment_matrix 单元格都非空

    注：success_criteria 的"是否真可量化"是**内容性**判断，由 experiment-planner 的
    Self-Reflection Checklist 自查；本函数只兜底"字段存在 + 非空"。

    注：hypothesis_coverage / innovation_points 相关 cross-ref 留 iterate 阶段兜底
    （这些字段在 method_design 里，不在本函数签名里——按职责单一原则不跨脚本传参）。

    Returns:
        True 通过 / False 失败。
    """
    warnings: list[str] = []

    baselines = experiment_plan.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        warnings.append("baselines 空")

    metrics = experiment_plan.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        warnings.append("metrics 空")

    matrix = experiment_plan.get("experiment_matrix")
    if not isinstance(matrix, list) or not matrix:
        warnings.append("experiment_matrix 空")
    else:
        for i, row in enumerate(matrix):
            if not isinstance(row, list) or not row:
                warnings.append(f"experiment_matrix[{i}] 空")
                continue
            for j, cell in enumerate(row):
                if not isinstance(cell, str) or not cell.strip():
                    warnings.append(f"experiment_matrix[{i}][{j}] 单元格空")

    for w in warnings:
        log.warning(f"[check_feasibility] experiment_matrix 警告: {w}")
    return not warnings


def _check_data_plan_realism(data_plan: dict) -> bool:
    """
    业务校验 data_plan 的"真实性"（区别于 validate_plan.py 的字段契约校验）。

    2026-08-29 v2 重构：split_strategy / expected_size / preprocessing_pipeline
    降为可选；新增必填 usage_plan。
    校验项：
        1. datasets 非空 + 每个 dataset.source_url 非空（防虚构数据）
        2. usage_plan 非空（v2 新增必填）
        3. split_strategy 若存在则 seed 必须有（保证可复现，v1 强校验）
        4. expected_size 若存在则三字段齐（rows / disk_gb / gpu_estimate）

    Returns:
        True 通过 / False 失败。
    """
    warnings: list[str] = []

    # 1. datasets 非空
    datasets = data_plan.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        warnings.append("datasets 空或不是 list")
    else:
        # 2. 每个 dataset.source_url 非空
        for i, ds in enumerate(datasets):
            if not isinstance(ds, dict):
                warnings.append(f"datasets[{i}] 不是 dict")
                continue
            if not ds.get("source_url"):
                warnings.append(f"datasets[{i}].source_url 空（防虚构数据）")

    # 2. usage_plan 必填（v2 新增）
    usage_plan = data_plan.get("usage_plan")
    if not isinstance(usage_plan, str) or not usage_plan.strip():
        warnings.append("usage_plan 缺失或为空（v2 必填，承载『数据怎么用』语义）")

    # 3. split_strategy 若存在则 seed 必填（v1 强校验保留为"若存在"）
    split_strategy = data_plan.get("split_strategy")
    if split_strategy is not None:
        if not isinstance(split_strategy, dict):
            warnings.append("split_strategy 不是 dict")
        elif not split_strategy.get("seed"):
            warnings.append("split_strategy.seed 缺失（保证可复现）")

    # 4. expected_size 若存在则三字段齐
    expected_size = data_plan.get("expected_size")
    if expected_size is not None:
        if not isinstance(expected_size, dict):
            warnings.append("expected_size 不是 dict")
        else:
            for f in ("rows", "disk_gb", "gpu_estimate"):
                if f not in expected_size:
                    warnings.append(f"expected_size.{f} 缺失")

    for w in warnings:
        log.warning(f"[check_feasibility] data_plan 警告: {w}")
    return not warnings


def produce_execution_config(
    output_dir: str,
    user_seeds: list[int] | None = None,
    user_max_retries: int | None = None,
    user_timeout_seconds: int | None = None,
    user_dry_run: bool | None = None,
) -> dict:
    """
    产出模块二的 ExecutionConfig（planning-schemas.md §2.5）。

    路径策略（schemas §2.5 + §5 一致性约束）：
        - run_dir：用户未指定时兜底为 `<output_dir>/run`
        - result_dir：用户未指定时兜底为 `<output_dir>/results`
        - 兜底后 result_dir 解析后必然位于 run_dir 内（满足一致性约束 #6）

    Args:
        output_dir:        main.py 传入的 --output-dir；run_dir / result_dir 默认基于其下子目录
        user_seeds:        用户覆盖 seeds（None 时用 [42]）
        user_max_retries:  用户覆盖 max_retries（None 时用 2）
        user_timeout_seconds: 用户覆盖 timeout_seconds（None 时用 null）
        user_dry_run:      用户覆盖 dry_run（None 时用 false）

    Returns:
        ExecutionConfig dict（含全部 6 字段）。
    """
    out = Path(output_dir)
    run_dir = str((out / "run").resolve())
    result_dir = str((out / "run" / "results").resolve())

    ec: dict = {
        "run_dir": run_dir,
        "result_dir": result_dir,
        "seeds": list(user_seeds) if user_seeds else list(_DEFAULT_SEEDS),
        "max_retries": user_max_retries if user_max_retries is not None else _DEFAULT_MAX_RETRIES,
        "timeout_seconds": (
            user_timeout_seconds
            if user_timeout_seconds is not None
            else _DEFAULT_TIMEOUT_SECONDS
        ),
        "dry_run": user_dry_run if user_dry_run is not None else _DEFAULT_DRY_RUN,
    }
    return ec
