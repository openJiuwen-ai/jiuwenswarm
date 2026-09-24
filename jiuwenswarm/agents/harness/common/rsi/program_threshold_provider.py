"""Stop in-flight program searches when an adopted candidate solves the task."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Awaitable, Callable

from openjiuwen.rsi.artifact_rsi.program_opt import PuctProgramArtifactProvider
from openjiuwen.rsi.events import EventNode

logger = logging.getLogger(__name__)
DEFAULT_SOLVED_THRESHOLD = 0.999


def _solved_threshold(run_dir: str | Path) -> float | None:
    """Read the provider's staged card, including its default threshold."""

    try:
        card = json.loads((Path(run_dir) / "scorecard.json").read_text(encoding="utf-8"))
        scorecard = card.get("scorecard", card)
        threshold = float(scorecard.get("solvedThreshold") or DEFAULT_SOLVED_THRESHOLD)
        return threshold if math.isfinite(threshold) else None
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("无法读取程序优化的达标阈值: %s", run_dir)
        return None


class ThresholdStoppingProgramProvider(PuctProgramArtifactProvider):
    """Complete a solved run and signal its other workers to stop.

    The Provider's stop flag prevents further expansions and lets active model
    waits wind down. Signal it without turning a solved task into a user
    termination. A sandbox evaluation already in progress may still finish.
    """

    async def run(self, request: Any, on_event: Any = None) -> Any:
        return await self._with_threshold_stop(request, on_event, super().run)

    async def resume(self, request: Any, on_event: Any = None) -> Any:
        return await self._with_threshold_stop(request, on_event, super().resume)

    async def _with_threshold_stop(
        self,
        request: Any,
        on_event: Any,
        operation: Callable[..., Awaitable[Any]],
    ) -> Any:
        threshold: float | None = None
        threshold_loaded = False

        async def observe(event: Any) -> None:
            nonlocal threshold, threshold_loaded
            if isinstance(event, EventNode):
                node = event.node
                if node.type == "adopted" and node.adopted and node.score is not None:
                    if not threshold_loaded:
                        threshold = _solved_threshold(request.run_dir)
                        threshold_loaded = True
                    if threshold is not None and math.isfinite(float(node.score)) and node.score >= threshold:
                        self._complete_solved_run(request.task_id)
            if on_event is not None:
                await on_event(event)

        return await operation(request, on_event=observe)

    def _complete_solved_run(self, task_id: str) -> None:
        # The upstream Provider guards both maps with this lock. Preserve a
        # concurrent user pause/terminate: an already-set flag wins.
        with self._lock:
            stop = self._stopping.get(task_id)
            state = self._live.get(task_id)
            if stop is None or state is None or stop.is_set():
                return
            state.stopped_status = "completed"
            stop.set()


__all__ = ["ThresholdStoppingProgramProvider"]
