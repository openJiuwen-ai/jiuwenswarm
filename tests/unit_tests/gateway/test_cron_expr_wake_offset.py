"""Tests for delay_seconds wake_offset clamp."""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.cron_expr import clamp_wake_offset_for_delay_seconds


@pytest.mark.parametrize(
    ("wake", "delay", "expected"),
    [
        (300, 60, 60),
        (300, 60.9, 60),
        (300, 600, 300),
        (10, 60, 10),
        (0, 60, 0),
        (None, 60, 0),  # missing → default 0（到点执行），clamp 仍为 0
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
