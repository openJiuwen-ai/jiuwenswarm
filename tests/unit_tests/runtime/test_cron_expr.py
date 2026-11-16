from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.runtime.cron.cron_expr import (
    clamp_wake_offset_for_delay_seconds,
    is_croniter_no_next_date,
    iso_to_one_shot_cron,
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
        iso_to_one_shot_cron(at_iso, timezone=timezone)


@pytest.mark.parametrize(
    ("at_iso", "timezone"),
    [
        ("2027-10-31T02:30:00", "Europe/Paris"),
        ("2026-11-01T01:30:00", "America/New_York"),
        ("2027-04-04T01:45:00", "Australia/Lord_Howe"),
    ],
)
def test_one_shot_reminder_rejects_ambiguous_local_time(
    at_iso: str,
    timezone: str,
) -> None:
    # The wall clock names two instants; picking one silently would be a guess.
    with pytest.raises(ValueError, match="ambiguous in timezone"):
        iso_to_one_shot_cron(at_iso, timezone=timezone)


@pytest.mark.parametrize(
    ("at_iso", "timezone", "expected"),
    [
        (
            "2027-03-28T01:30:00",
            "Europe/Paris",
            ("0 30 1 28 3 ? 2027", "Europe/Paris"),
        ),
        (
            "2027-03-28T03:30:00",
            "Europe/Paris",
            ("0 30 3 28 3 ? 2027", "Europe/Paris"),
        ),
        (
            "2027-03-28T02:30:00",
            "Europe/Istanbul",
            ("0 30 2 28 3 ? 2027", "Europe/Istanbul"),
        ),
        (
            "2027-03-28T01:30:00Z",
            "Europe/Paris",
            ("0 30 3 28 3 ? 2027", "Europe/Paris"),
        ),
        (
            "2027-03-28T02:30:00+01:00",
            "Europe/Paris",
            ("0 30 3 28 3 ? 2027", "Europe/Paris"),
        ),
        # First pass of the repeated hour: the local expression names it.
        (
            "2026-11-01T01:30:00-04:00",
            "America/New_York",
            ("0 30 1 1 11 ? 2026", "America/New_York"),
        ),
        # Second pass: a local expression would fire an hour early, so the
        # instant is rendered in UTC instead.
        (
            "2026-11-01T01:30:00-05:00",
            "America/New_York",
            ("0 30 6 1 11 ? 2026", "UTC"),
        ),
        (
            "2026-11-01T06:30:00Z",
            "America/New_York",
            ("0 30 6 1 11 ? 2026", "UTC"),
        ),
        # 30-minute fall-back (Lord Howe): 01:45 +10:30 is the second pass,
        # i.e. 2027-04-03 15:15Z.
        (
            "2027-04-04T01:45:00+10:30",
            "Australia/Lord_Howe",
            ("0 15 15 3 4 ? 2027", "UTC"),
        ),
        # No DST: the representation must stay byte-identical to before.
        (
            "2026-07-24T18:25:31+08:00",
            "Asia/Shanghai",
            ("31 25 18 24 7 ? 2026", "Asia/Shanghai"),
        ),
    ],
)
def test_one_shot_reminder_preserves_valid_time_conversion(
    at_iso: str,
    timezone: str,
    expected: tuple[str, str],
) -> None:
    assert iso_to_one_shot_cron(at_iso, timezone=timezone) == expected


@pytest.mark.parametrize(
    ("at_iso", "timezone"),
    [
        ("2027-03-28T01:30:00", "Europe/Paris"),
        ("2027-03-28T02:30:00+01:00", "Europe/Paris"),
        ("2026-11-01T00:59:59-04:00", "America/New_York"),
        # First pass of the repeated hour: the local expression must resolve to
        # fold=0, otherwise the reminder lands an hour late.
        ("2026-11-01T01:30:00-04:00", "America/New_York"),
        ("2026-11-01T05:30:00Z", "America/New_York"),
        ("2026-11-01T01:30:00-05:00", "America/New_York"),
        ("2027-04-04T01:45:00+10:30", "Australia/Lord_Howe"),
        ("2026-07-24T18:25:31+08:00", "Asia/Shanghai"),
    ],
)
def test_one_shot_cron_fires_at_the_requested_instant(
    at_iso: str,
    timezone: str,
) -> None:
    """The scheduler's next occurrence is the instant the caller asked for."""
    cron_expr, cron_timezone = iso_to_one_shot_cron(at_iso, timezone=timezone)
    target = datetime.fromisoformat(at_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=ZoneInfo(timezone))
    base = datetime.fromtimestamp(
        target.timestamp() - 1, tz=ZoneInfo(cron_timezone)
    )
    assert next_cron_datetime(cron_expr, base).timestamp() == target.timestamp()


def test_one_shot_cron_utc_fallback_covers_the_repeated_hour_window() -> None:
    cron_expr, cron_timezone = iso_to_one_shot_cron(
        "2026-11-01T01:30:00-05:00", timezone="America/New_York"
    )

    assert (cron_expr, cron_timezone) == ("0 30 6 1 11 ? 2026", "UTC")
    # Between the first (05:30Z) and second (06:30Z) occurrence the old local
    # expression had no next date at all; the UTC one still lands on 06:30Z.
    assert next_cron_datetime(
        cron_expr, datetime(2026, 11, 1, 5, 45, tzinfo=UTC)
    ) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


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
