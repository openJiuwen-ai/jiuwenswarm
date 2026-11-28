# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monotonic clock base and early startup marks.

Only the standard library may be imported here: the Worker records these marks
before Agent Runtime (and therefore OpenTelemetry) is loaded.
"""

from __future__ import annotations

import time


class ClockBase:
    """One wall/monotonic pair used to convert stage timestamps.

    ``early_marks`` stays a plain list of ``(name, mono_ns)`` entries so the
    Worker can record phases before any OTel object exists. ``replayed`` is
    stored on the instance because normal attach and failure attach share the
    same clock. ``failed_phase`` marks a phase that never completed, so the
    replayed span can carry an error outcome instead of disappearing.
    """

    def __init__(self) -> None:
        self.wall_ns = time.time_ns()
        self.mono_ns = time.monotonic_ns()
        self.early_marks: list[tuple[str, int]] = [("main_entry", self.mono_ns)]
        self.replayed = False
        self.failed_phase: str | None = None

    def mark(self, name: str) -> None:
        self.early_marks.append((name, time.monotonic_ns()))

    def to_unix_nano(self, mono_ns: int) -> int:
        return self.wall_ns + (mono_ns - self.mono_ns)
