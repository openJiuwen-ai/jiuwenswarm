from __future__ import annotations

from datetime import datetime

from zoneinfo import ZoneInfo


def cron_field_count(expr: str) -> int:
    return len(str(expr or "").split())


def iso_to_seven_field_cron(at_iso: str, *, timezone: str) -> str:
    """Convert ISO8601 datetime into croniter 7-field cron:
    minute hour day month dow second year.

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
    # day-of-week is left as '*' because we fixed year/month/day.
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} * {dt.second} {dt.year}"


def validate_cron_expression(expr: str, *, timezone: str) -> None:
    """Validate cron expression format is 5 or 7 fields and croniter accepts it.

    Note: for 7-field one-shot with a fixed past year, `croniter.get_next()`
    can fail; we only validate syntax here.
    """
    from croniter import croniter  # type: ignore

    raw = str(expr or "").strip()
    if not raw:
        raise ValueError("cron_expr is empty")

    n = cron_field_count(raw)
    if n not in (5, 7):
        raise ValueError(
            f"cron_expr must have 5 fields or 7 fields, got {n} fields"
        )
    if not croniter.is_valid(raw):
        raise ValueError("invalid cron expression")
    # Ensure timezone itself is valid. Do not require a future matching date.
    _ = ZoneInfo(timezone)
    croniter(raw, datetime.now(tz=ZoneInfo(timezone)))
