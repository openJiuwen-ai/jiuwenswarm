# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ClockBase and early mark replay."""

from __future__ import annotations

import time

from jiuwenswarm.runtime_trace.clock import ClockBase


def test_unix_conversion_is_linear() -> None:
    clock = ClockBase()
    before = time.time_ns()
    converted_now = clock.to_unix_nano(time.monotonic_ns())
    after = time.time_ns()
    assert before <= converted_now <= after


def test_marks_are_monotonic_and_named() -> None:
    clock = ClockBase()
    clock.mark("config")
    clock.mark("ipc_listening")
    names = [name for name, _ in clock.early_marks]
    assert names == ["main_entry", "config", "ipc_listening"]
    stamps = [stamp for _, stamp in clock.early_marks]
    assert stamps == sorted(stamps)


def test_replayed_defaults_to_false() -> None:
    assert ClockBase().replayed is False
