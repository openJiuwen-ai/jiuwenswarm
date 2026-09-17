from __future__ import annotations

from datetime import datetime

from zoneinfo import ZoneInfo


def cron_field_count(expr: str) -> int:
    return len(str(expr or "").split())


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
        raise ValueError(
            f"invalid cron expression: '{raw}'"
        )
    _ = ZoneInfo(timezone)
    croniter(normalized, datetime.now(tz=ZoneInfo(timezone)), second_at_beginning=True)
