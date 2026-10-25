from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.runtime.cron.cron_expr import (
    clamp_wake_offset_for_delay_seconds,
    is_croniter_no_next_date,
    next_cron_datetime,
    validate_cron_expression,
)


@pytest.mark.parametrize(
    "expression",
    [
        "15 9 * * 1-5",
        "30 15 9 * * ? *",
        "30 15 9 5 9 ? 2099",
    ],
)
def test_validate_cron_expression_accepts_shared_five_and_seven_field_syntax(
    expression: str,
) -> None:
    validate_cron_expression(expression, timezone="Asia/Shanghai")


def test_next_cron_datetime_preserves_seconds_for_seven_fields() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 9, 15, 20, tzinfo=timezone)

    assert next_cron_datetime("30 15 9 * * ? *", base) == datetime(
        2026, 9, 5, 9, 15, 30, tzinfo=timezone
    )


@pytest.mark.parametrize(
    ("wake", "delay", "expected"),
    [
        (300, 60, 60),
        (300, 60.9, 60),
        (300, 600, 300),
        (10, 60, 10),
        (0, 60, 0),
        (None, 60, 0),
        (None, 600, 0),
        (-5, 60, 0),
        (300, 1, 1),
        (300, 0.5, 0),
    ],
)
def test_clamp_wake_offset_for_delay_seconds(wake, delay, expected) -> None:
    assert clamp_wake_offset_for_delay_seconds(wake, delay) == expected


def test_clamp_wake_offset_invalid_wake_falls_back_to_default() -> None:
    assert clamp_wake_offset_for_delay_seconds("bad", 60) == 0


def test_clamp_wake_offset_non_positive_delay() -> None:
    assert clamp_wake_offset_for_delay_seconds(300, 0) == 0
    assert clamp_wake_offset_for_delay_seconds(300, -10) == 0


def test_is_croniter_no_next_date_recognizes_exhausted_expression() -> None:
    assert is_croniter_no_next_date(RuntimeError("failed to find next date"))
    assert is_croniter_no_next_date(ValueError("other")) is False


def test_next_cron_datetime_supports_far_future_fixed_year() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 9, 15, 20, tzinfo=timezone)

    assert next_cron_datetime("30 15 9 5 9 ? 2099", base) == datetime(
        2099, 9, 5, 9, 15, 30, tzinfo=timezone
    )
