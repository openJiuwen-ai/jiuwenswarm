# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-stage timing for a linear sequence of steps on a hot path.

Written for the places where a single log line reports one aggregate number
("prepare_ms=136.1") and the next question is always "which step". Marking each
step turns that one number into a breakdown without scattering timestamp
bookkeeping through the code being measured.

The renderer emits stages in execution order and keeps the whole breakdown on
one line, so a slow turn can be read straight out of the log stream.
"""

from __future__ import annotations

# Bound as a module attribute rather than used as ``time.monotonic`` so tests can
# substitute a deterministic clock here without touching the global ``time``
# module — patching that would also retime asyncio timers and socket timeouts.
from time import monotonic


class StageTimer:
    """Accumulate elapsed time per named stage of one pass through a code path.

    Not thread-safe and not reusable across passes: construct one per pass, at
    the point where the measured work begins.
    """

    def __init__(self) -> None:
        """Start the clock for the first stage."""
        self._started_at = monotonic()
        self._stage_started_at = self._started_at
        self._stages: list[tuple[str, float]] = []

    def mark(self, stage: str) -> None:
        """Close the current stage and open the next one.

        Args:
            stage: Name of the stage that just finished. Repeat names are kept
                as separate entries rather than merged, so a loop that marks the
                same name shows each pass.
        """
        now = monotonic()
        self._stages.append((stage, (now - self._stage_started_at) * 1000))
        self._stage_started_at = now

    def total_ms(self) -> float:
        """Return milliseconds elapsed since construction.

        Returns:
            Total elapsed time, including any work after the last ``mark``.
        """
        return (monotonic() - self._started_at) * 1000

    def render(self, *, slowest_first: bool = False) -> str:
        """Render the recorded stages as a single-line ``name=ms`` breakdown.

        Args:
            slowest_first: Order stages by descending duration instead of
                execution order. Useful when the caller only logs a breakdown
                because something was slow.

        Returns:
            Space-separated ``name=ms`` pairs, empty when nothing was marked.
        """
        stages = list(self._stages)
        if slowest_first:
            stages.sort(key=lambda item: item[1], reverse=True)
        return " ".join(f"{name}={elapsed_ms:.1f}" for name, elapsed_ms in stages)


__all__ = ["StageTimer"]
