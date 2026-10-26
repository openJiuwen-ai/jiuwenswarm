from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


_MAX_YEARS_BETWEEN_MATCHES = 130
# 提前唤醒默认量：0 = 到点执行。任务没填这个数时用它。
_DEFAULT_WAKE_OFFSET_SECONDS = 0


def cron_field_count(expr: str) -> int:
    return len(str(expr or "").split())


def clamp_wake_offset_for_delay_seconds(
    wake_offset_seconds: Any,
    delay_seconds: float,
    *,
    default_when_missing: int = _DEFAULT_WAKE_OFFSET_SECONDS,
) -> int:
    """把提前唤醒的秒数缩小到不超过「离下次触发还剩多少秒」。

    只跑一次的任务如果提前量比剩余等待时间还大，唤醒时刻会落在过去，
    调度器会立刻误触发。delay 不是正数时直接返回 0（到点执行）。
    """
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
        raise ValueError(f"invalid cron expression: '{raw}'")
    _ = ZoneInfo(timezone)
    croniter(normalized, datetime.now(tz=ZoneInfo(timezone)), second_at_beginning=True)


def is_croniter_no_next_date(exc: Exception) -> bool:
    """表达式算不出下一次。常见于年份已经过去的一次性任务，这时视为过期。"""
    return (
        exc.__class__.__name__ == "CroniterBadDateError"
        or "failed to find next date" in str(exc)
    )


def next_cron_datetime(cron_expr: str, base_dt: datetime) -> datetime:
    """Return the next occurrence for a supported 5- or 7-field expression."""
    from croniter import croniter  # type: ignore

    normalized = normalize_cron_expr(cron_expr)
    value = croniter(
        normalized,
        base_dt,
        second_at_beginning=True,
        max_years_between_matches=_MAX_YEARS_BETWEEN_MATCHES,
    ).get_next(datetime)
    if not isinstance(value, datetime):
        raise RuntimeError("croniter returned invalid datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=base_dt.tzinfo)
    return value
