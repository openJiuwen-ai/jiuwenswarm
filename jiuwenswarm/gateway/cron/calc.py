# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Cron 纯计算模块：唯一 croniter 入口。

本模块不依赖任何存储 / 业务代码（不 import scheduler、db_schedule、store 等），
是"计算"的唯一来源。内存层（scheduler）与隔离层（db_schedule）都从这里取
"下一趟触发时间"，避免同一套 croniter 逻辑在多处漂移。

规则：任何需要"根据表达式算出下一个时间点"的地方，只能 import 本模块。
存储语义（如防积压、持久化 next_run_at、抢锁）一律不属于这里。
"""

from __future__ import annotations

from datetime import datetime


def cron_next_push_dt(cron_expr: str, base_dt: datetime) -> datetime:
    """由 ``base_dt`` 起算下一次触发时刻（tz-aware）。

    - 兼容 Quartz 7 段格式（秒 分 时 日 月 周 年）；
    - croniter 默认 6 段格式为 分 时 日 月 周 [秒] [年]。
    """
    # Lazy import so the rest of the system can still run without cron enabled.
    from croniter import croniter  # type: ignore

    field_count = len(cron_expr.strip().split())
    second_at_beginning = field_count == 7
    it = croniter(cron_expr, base_dt, second_at_beginning=second_at_beginning)
    nxt = it.get_next(datetime)
    if not isinstance(nxt, datetime):
        raise RuntimeError("croniter returned invalid datetime")
    if nxt.tzinfo is None:
        # Keep tz-consistent; base_dt is tz-aware in our usage.
        return nxt.replace(tzinfo=base_dt.tzinfo)
    return nxt


def cron_prev_push_dt(cron_expr: str, base_dt: datetime) -> datetime:
    """由 ``base_dt`` 起算上一次触发时刻（tz-aware）。

    供内存层"错过窗口补偿"使用：single-shot 因重启/卡顿迟到若干秒时，
    用上一次触发时刻判断是否仍在补偿窗口内。
    """
    from croniter import croniter  # type: ignore

    field_count = len(cron_expr.strip().split())
    second_at_beginning = field_count == 7
    it = croniter(cron_expr, base_dt, second_at_beginning=second_at_beginning)
    prev = it.get_prev(datetime)
    if not isinstance(prev, datetime):
        raise RuntimeError("croniter returned invalid datetime")
    if prev.tzinfo is None:
        return prev.replace(tzinfo=base_dt.tzinfo)
    return prev


def is_croniter_no_next_date(exc: Exception) -> bool:
    """croniter 找不到下一次日期（通常为单次 year 固定为过去）时视为过期。"""
    return (
        exc.__class__.__name__ == "CroniterBadDateError"
        or "failed to find next date" in str(exc)
    )


__all__ = (
    "cron_next_push_dt",
    "cron_prev_push_dt",
    "is_croniter_no_next_date",
)
