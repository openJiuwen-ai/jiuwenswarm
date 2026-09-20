from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.runtime.cron.cron_expr import (
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


def test_next_cron_datetime_supports_far_future_fixed_year() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 9, 15, 20, tzinfo=timezone)

    assert next_cron_datetime("30 15 9 5 9 ? 2099", base) == datetime(
        2099, 9, 5, 9, 15, 30, tzinfo=timezone
    )
