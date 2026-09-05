from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

# Harness / CronJob 常见默认提前量；相对 one-shot 时不可超过 delay。
_DEFAULT_WAKE_OFFSET_SECONDS = 300


def cron_field_count(expr: str) -> int:
    return len(str(expr or "").split())


def clamp_wake_offset_for_delay_seconds(
    wake_offset_seconds: Any,
    delay_seconds: float,
    *,
    default_when_missing: int = _DEFAULT_WAKE_OFFSET_SECONDS,
) -> int:
    """相对 one-shot：将 wake_offset 收敛到不超过 delay。"""
    delay = float(delay_seconds)
    if delay <= 0:
        return 0
    if wake_offset_seconds is None:
        requested = int(default_when_missing)
    else:
        try:
            requested = int(wake_offset_seconds)
        except (TypeError, ValueError):
            requested = int(default_when_missing)
    if requested < 0:
        requested = 0
    max_allowed = max(0, math.floor(delay))
    return min(requested, max_allowed)


def normalize_cron_expr(raw: str) -> str:
    """Normalize cron expression to 7-field Quartz format.

    5-field (minute hour day month dow) → prepend "0" (second) and append "*" (year).
    7-field is left unchanged.
    Other field counts raise ValueError.
    """
    s = str(raw or "").strip()
    n = cron_field_count(s)
    if n == 5:
        return f"0 {s} *"
    if n == 7:
        return s
    raise ValueError(
        f"cron_expr must have 5 or 7 fields, got {n} fields. "
        "5-field: minute hour day month dow. "
        "7-field (Quartz): second minute hour day month dow year."
    )


def is_oneshot_cron_expr(expr: str) -> bool:
    """判断 7 字段 Quartz 表达式是否为「单次」，与前端 ``cronExprToSchedule`` 的
    ``once`` 判定逐条对齐：

    - 秒/分/时/日/月 均为单个非负整数（前端 ``parseSingleInt`` 即 ``/^\\d+$/``）；
    - 周字段为通配（``*`` 或 ``?``，前端 ``isWildcard``）；
    - 年份为单个固定整数（非通配）。

    仅用于在 AgentServer 侧创建任务时补全缺失的 ``delete_after_run``，
    使对话创建与手动创建的规格对齐。
    """
    parts = str(expr or "").strip().split()
    if len(parts) != 7:
        return False
    second, minute, hour, day, month, week, year = parts

    def _wildcard(field: str) -> bool:
        return field in ("*", "?")

    def _single_int(field: str) -> bool:
        # 与前端 parseSingleInt(/^\d+$/) 等价：仅 [0-9]+
        return re.fullmatch(r"[0-9]+", field) is not None

    if _wildcard(year):
        return False
    if not _wildcard(week):
        return False
    return all(
        _single_int(f)
        for f in (second, minute, hour, day, month, year)
    )


def iso_to_seven_field_cron(at_iso: str, *, timezone: str) -> str:
    """Convert ISO8601 datetime into 7-field cron (Quartz format):
    second minute hour day month dow year.

    If the input has no timezone, interpret it in `timezone`.
    """
    s = (at_iso or "").strip()
    if not s:
        raise ValueError("at_iso is empty")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    tz = ZoneInfo(timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return f"{dt.second} {dt.minute} {dt.hour} {dt.day} {dt.month} ? {dt.year}"


def validate_cron_expression(expr: str, *, timezone: str) -> None:
    """Validate cron expression (5-field or 7-field Quartz format).

    5-field (minute hour day month dow) is auto-normalized to 7-field by prepending
    second=0 and appending year=*.

    Note: for 7-field one-shot with a fixed past year, `croniter.get_next()`
    can fail; we only validate syntax here.
    """
    from croniter import croniter  # type: ignore

    raw = str(expr or "").strip()
    if not raw:
        raise ValueError("cron_expr is empty")

    normalized = normalize_cron_expr(raw)

    # Use second_at_beginning=True for Quartz 7-field format
    if not croniter.is_valid(normalized, second_at_beginning=True):
        raise ValueError(
            f"invalid cron expression: '{raw}'"
        )
    _ = ZoneInfo(timezone)
    croniter(normalized, datetime.now(tz=ZoneInfo(timezone)), second_at_beginning=True)
