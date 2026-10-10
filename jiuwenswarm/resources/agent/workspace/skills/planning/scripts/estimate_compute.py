# -*- coding: utf-8 -*-
"""
estimate_compute.py — 算力估算与预算门禁判定（骨架）

在阶段三实现：根据 ExperimentPlan.compute_estimate.steps 与 resource_constraints 比对，
按 references/planning-schemas.md §6 三档降级规则给出档位判定，
供 Leader 决定是否调用降级重排策略。

当前为骨架：仅定义 CLI 入口与三档降级常量，接口先行、实现后补。

示例（阶段三后可用）:
    python scripts/planning/estimate_compute.py --estimate path/to/compute.json --budget 5000 --unit gpu-h
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.estimate_compute")

# references/planning-schemas.md §6 三档降级常量的脚本侧镜像
TIER_FULL = 0       # 精算档: total <= budget，全量实验
TIER_TRIM = 1       # 消减档: 超 <= 25%，砍非核心消融
TIER_CORE = 2       # 核心优先档: 超 > 25%，只保留核心创新点最小实验集
OVER_BUDGET_TRIM_RATIO = 0.25


def judge_tier(total_cost: float, budget: float | None) -> tuple[int, float]:
    """
    返回 (档位, 超支比例)。超支比例 = (total - budget) / budget；未超时为 0。

    Args:
        total_cost: 实验方案估算的算力（float；与 budget 同单位）。
        budget:     资源预算（float 或 None）。语义：
                      - None  → 用户不设算力门禁，直接 TIER_FULL（不触发降级）
                      - 0     → 零预算；若 total_cost > 0 必超预算，触发 TIER_CORE
                      - > 0   → 按 (total - budget) / budget 计算超支比例

    Returns:
        (tier, over_ratio):
            tier:       档位（TIER_FULL=0 / TIER_TRIM=1 / TIER_CORE=2）
            over_ratio: 超支比例；budget=None 或 total≤budget 时为 0.0；
                        budget=0 且 total>0 时为 +inf（一定超预算）。
    """
    # 算力门禁未启用 → 永远精算
    if budget is None:
        return TIER_FULL, 0.0
    # 预算充足 → 精算
    if total_cost <= budget:
        return TIER_FULL, 0.0
    # 超预算：算超支比例（budget=0 时取 +inf）
    over_ratio = (total_cost - budget) / budget if budget > 0 else float("inf")
    tier = TIER_TRIM if over_ratio <= OVER_BUDGET_TRIM_RATIO else TIER_CORE
    return tier, over_ratio


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="算力估算与预算门禁判定（骨架）")
    parser.add_argument("--estimate", required=True, help="compute_estimate 文件路径（JSON）")
    parser.add_argument(
        "--budget",
        type=str,
        default="null",
        help="算力预算（与 estimate 同单位）。传数字或不传 'null'；'null' 表示不设算力门禁",
    )
    args = parser.parse_args(argv)

    if args.budget.strip().lower() in ("null", "none", ""):
        budget: float | None = None
    else:
        budget = float(args.budget)

    estimate = _load_json(args.estimate)
    total = float(estimate.get("total_cost", 0.0))
    tier, over_ratio = judge_tier(total, budget)

    if budget is None:
        log.info(f"total_cost={total}, budget=None（无算力门禁）")
        log.info("over_ratio=0.00%")
    else:
        log.info(f"total_cost={total}, budget={budget}")
        if over_ratio == float("inf"):
            log.warning("over_ratio=+inf（budget=0 且 total>0 必超预算）")
        else:
            log.info(f"over_ratio={over_ratio:.2%}")
    tier_name = {TIER_FULL: "精算", TIER_TRIM: "消减", TIER_CORE: "核心优先"}[tier]
    log.info(f"tier={tier} ({tier_name})")

    if tier == TIER_CORE:
        log.warning("提示: 超出预算 >25%，需启用核心优先降级（见 references/planning-schemas.md §6）")
    return 0


if __name__ == "__main__":
    sys.exit(main())