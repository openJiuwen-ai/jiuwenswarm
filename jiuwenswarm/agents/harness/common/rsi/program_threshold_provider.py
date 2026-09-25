"""Stop in-flight program searches when an adopted candidate solves the task."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from openjiuwen.rsi.artifact_rsi.program_opt import PuctProgramArtifactProvider
from openjiuwen.rsi.artifact_rsi.program_opt.scorecard import solved_threshold
from openjiuwen.rsi.events import EventNode

logger = logging.getLogger(__name__)


def _solved_threshold(run_dir: str | Path) -> float | None:
    """Read the provider's staged card, including its default threshold."""

    try:
        card = json.loads((Path(run_dir) / "scorecard.json").read_text(encoding="utf-8"))
        scorecard = card.get("scorecard", card)
        threshold = solved_threshold(scorecard)
        return threshold if math.isfinite(threshold) else None
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("无法读取程序优化的达标阈值: %s", run_dir)
        return None


class ThresholdStoppingProgramProvider(PuctProgramArtifactProvider):
    """Complete a solved run and signal its other workers to stop.

    The pinned Provider already prevents *new* expansions after a solved node.
    Its stop flag also lets active model waits and the search thread wind down;
    this adapter signals that flag without turning a solved task into a user
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
        solved = False

        async def observe(event: Any) -> None:
            nonlocal threshold, threshold_loaded, solved
            if isinstance(event, EventNode):
                node = event.node
                if node.type == "adopted" and node.adopted and node.score is not None:
                    if not threshold_loaded:
                        threshold = _solved_threshold(request.run_dir)
                        threshold_loaded = True
                    if threshold is not None and math.isfinite(float(node.score)) and node.score >= threshold:
                        solved = self._complete_solved_run(request.task_id) or solved
                elif solved and _is_stopped_empty_candidate(node):
                    # The pinned engine returns an empty reply for model waits
                    # abandoned by the stop flag. Its durable tree calls those
                    # attempts rejected, but they were stopped by a winner.
                    # Mark the service projection so a later tree refresh can
                    # keep the same verdict instead of restoring "failed".
                    extra = dict(node.extra or {})
                    program = extra.get("program")
                    if isinstance(program, dict):
                        extra["program"] = {
                            **program, "logical_kind": "pruned", "error": None,
                        }
                    extra["threshold_stop_cancelled"] = True
                    event = replace(event, node=replace(
                        node,
                        type="pruned",
                        summary="已达标，停止此候选",
                        reason="其他候选已达到目标分数",
                        failure_class=None,
                        extra=extra,
                    ))
            if on_event is not None:
                await on_event(event)

        return await operation(request, on_event=observe)

    def _complete_solved_run(self, task_id: str) -> bool:
        # The upstream Provider guards both maps with this lock. Preserve a
        # concurrent user pause/terminate: an already-set flag wins.
        with self._lock:
            stop = self._stopping.get(task_id)
            state = self._live.get(task_id)
            if stop is None or state is None or stop.is_set():
                return False
            state.stopped_status = "completed"
            stop.set()
            return True


def _is_stopped_empty_candidate(node: Any) -> bool:
    return (
        node.type in {"candidate", "rejected"}
        and not node.adopted
        and node.score is None
        and node.failure_class == "empty_reply"
    )


__all__ = ["ThresholdStoppingProgramProvider"]
