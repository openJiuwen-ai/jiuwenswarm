from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.runtime.cron.cron_expr import (
    iso_to_seven_field_cron,
    next_cron_datetime,
    validate_cron_expression,
)


@pytest.mark.parametrize(
    ("at_iso", "timezone"),
    [
        ("2027-03-28T02:30:00", "Europe/Paris"),
        ("2027-03-14T02:30:00", "America/New_York"),
        ("2027-10-03T02:15:00", "Australia/Lord_Howe"),
    ],
)
def test_one_shot_reminder_rejects_nonexistent_local_time(
    at_iso: str,
    timezone: str,
) -> None:
    with pytest.raises(ValueError, match="does not exist in timezone"):
        iso_to_seven_field_cron(at_iso, timezone=timezone)


@pytest.mark.parametrize(
    ("at_iso", "timezone", "expected"),
    [
        ("2027-03-28T01:30:00", "Europe/Paris", "0 30 1 28 3 ? 2027"),
        ("2027-03-28T03:30:00", "Europe/Paris", "0 30 3 28 3 ? 2027"),
        ("2027-03-28T02:30:00", "Europe/Istanbul", "0 30 2 28 3 ? 2027"),
        ("2027-03-28T01:30:00Z", "Europe/Paris", "0 30 3 28 3 ? 2027"),
        ("2027-03-28T02:30:00+01:00", "Europe/Paris", "0 30 3 28 3 ? 2027"),
        ("2027-10-31T02:30:00", "Europe/Paris", "0 30 2 31 10 ? 2027"),
    ],
)
def test_one_shot_reminder_preserves_valid_time_conversion(
    at_iso: str,
    timezone: str,
    expected: str,
) -> None:
    assert iso_to_seven_field_cron(at_iso, timezone=timezone) == expected


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


def test_next_cron_datetime_supports_far_future_fixed_year() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 9, 15, 20, tzinfo=timezone)

    assert next_cron_datetime("30 15 9 5 9 ? 2099", base) == datetime(
        2099, 9, 5, 9, 15, 30, tzinfo=timezone
    )
