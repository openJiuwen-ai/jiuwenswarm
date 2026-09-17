# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the per-stage hot-path timer."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.stage_timer import StageTimer


@pytest.fixture(name="fake_clock")
def _fake_clock(monkeypatch: pytest.MonkeyPatch):
    """Drive the timer off a hand-advanced clock so timings are exact."""
    now = {"t": 100.0}
    monkeypatch.setattr("jiuwenswarm.common.stage_timer.monotonic", lambda: now["t"])

    def advance(seconds: float) -> None:
        now["t"] += seconds

    return advance


def test_stages_render_in_execution_order(fake_clock) -> None:
    """The breakdown reads in the order the code ran, not by size."""
    timer = StageTimer()
    fake_clock(0.010)
    timer.mark("first")
    fake_clock(0.250)
    timer.mark("second")
    fake_clock(0.005)
    timer.mark("third")

    assert timer.render() == "first=10.0 second=250.0 third=5.0"


def test_slowest_first_reorders_by_duration(fake_clock) -> None:
    """The slow-path log wants the offender first."""
    timer = StageTimer()
    fake_clock(0.010)
    timer.mark("first")
    fake_clock(0.250)
    timer.mark("second")

    assert timer.render(slowest_first=True) == "second=250.0 first=10.0"


def test_total_includes_work_after_the_last_mark(fake_clock) -> None:
    """Trailing work must not vanish from the total."""
    timer = StageTimer()
    fake_clock(0.010)
    timer.mark("first")
    fake_clock(0.040)

    assert timer.total_ms() == pytest.approx(50.0)


def test_render_is_empty_when_nothing_was_marked(fake_clock) -> None:
    """A pass that fails before the first mark still renders cleanly."""
    timer = StageTimer()
    fake_clock(0.030)

    assert timer.render() == ""
    assert timer.total_ms() == pytest.approx(30.0)


def test_repeated_stage_names_are_kept_separate(fake_clock) -> None:
    """A loop marking one name shows each pass rather than merging them."""
    timer = StageTimer()
    fake_clock(0.001)
    timer.mark("step")
    fake_clock(0.002)
    timer.mark("step")

    assert timer.render() == "step=1.0 step=2.0"
