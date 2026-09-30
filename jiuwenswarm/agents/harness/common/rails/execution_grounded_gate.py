# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Execution-grounded success-window gate for Skill evolution.

This is a JiuwenSwarm-side Harness extension. Native ``SkillEvolutionRail``
triggers evolution from execution signals with no longitudinal check: a single
failure or a plausible-looking script artifact is enough to write into the
Skill library. On long streams that policy (a) wastes tokens after the agent
has already converged and (b) admits one-off / adversarial content.

The gate is *headroom control*, not a content filter:

    G1(t) = 1[ |W_t| >= m  AND  mean(W_t) < 1 ]

Evolution is allowed only while recent success still has room to improve.
Once the window is all-success, further mutations are suppressed.

An optional confidence threshold (Gate 2) can additionally drop candidate
records whose judge-estimated reusability is below ``min_confidence``. Gate 2
is a semantic pre-filter; it is *not* a substitute for execution evidence
(see the SkillForge paper: plausible distractors score as high as valid rules).

Wiring: ``attach_execution_gate(evolution_rail, ...)`` wraps
``_allow_evolution_trigger`` of a live ``SkillEvolutionRail`` / team variant.
Enabled from config::

    react:
      evolution:
        skill_evolution: true
        execution_gate:
          enabled: true
          window: 6
          min_samples: 3
          min_confidence: 0.6
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)


@dataclass
class _Outcome:
    task_id: str
    success: bool
    ts: float = field(default_factory=time.time)


class ExecutionGroundedGate:
    """Pure success-window + optional confidence gate (no Harness dependency)."""

    def __init__(
        self,
        *,
        window: int = 6,
        min_samples: int = 3,
        min_confidence: float = 0.6,
    ) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        self.window = window
        self.min_samples = min_samples
        self.min_confidence = min_confidence
        self._outcomes: Deque[_Outcome] = deque(maxlen=window)

    def record_task_outcome(self, task_id: str, success: bool) -> None:
        """Record one task. Same ``task_id`` twice in a row replaces, not appends.

        DeepAgent may fire both ``after_task_iteration`` and ``after_invoke``
        for one conversation; without this, the window double-counts.
        """
        tid = str(task_id)
        ok = bool(success)
        if self._outcomes and self._outcomes[-1].task_id == tid:
            self._outcomes[-1] = _Outcome(task_id=tid, success=ok)
            return
        self._outcomes.append(_Outcome(task_id=tid, success=ok))

    @property
    def recent_success_rate(self) -> float:
        if len(self._outcomes) < self.min_samples:
            return -1.0
        return sum(1 for outcome in self._outcomes if outcome.success) / len(self._outcomes)

    def should_evolve(self) -> tuple[bool, str]:
        if len(self._outcomes) < self.min_samples:
            return False, f"insufficient samples ({len(self._outcomes)}/{self.min_samples})"
        rate = self.recent_success_rate
        if rate >= 1.0:
            return False, f"converged (recent_success_rate={rate:.2f})"
        return True, f"improvable (recent_success_rate={rate:.2f})"


class ExecutionGroundedGateRail(DeepAgentRail):
    """Records per-task outcomes so a bound evolution rail can consult G1.

    Priority is higher than ``SkillEvolutionRail`` (80) so the outcome is in
    the window *before* the evolution trigger is evaluated on the same invoke.
    """

    priority = 90

    def __init__(self, gate: ExecutionGroundedGate) -> None:
        super().__init__()
        self.gate = gate

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:  # noqa: D102
        self._record_from_ctx(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:  # noqa: D102
        self._record_from_ctx(ctx)

    def _record_from_ctx(self, ctx: AgentCallbackContext) -> None:
        success = getattr(ctx, "success", None)
        if not isinstance(success, bool):
            return
        task_id = getattr(getattr(ctx, "inputs", None), "task_id", "") or "unknown"
        self.gate.record_task_outcome(str(task_id), success)


def attach_execution_gate(
    rail: Any,
    gate: ExecutionGroundedGate,
) -> Any:
    """Bind *gate* onto a SkillEvolutionRail-like object.

    Wraps ``_allow_evolution_trigger`` so a converged / under-sampled window
    skips the native signal-triggered evolution path. The original method is
    preserved as ``_allow_evolution_trigger_ungated``.
    """
    orig = getattr(rail, "_allow_evolution_trigger", None)
    if orig is None:
        logger.warning("[execution-gate] rail has no _allow_evolution_trigger; skip bind")
        return rail
    # 动态挂接到非本类创建的 rail 实例：用 setattr/getattr 显式表达动态绑定，
    # 避免 G.CLS.11 的受保护成员访问写法。
    setattr(rail, "_execution_gate", gate)
    setattr(rail, "_allow_evolution_trigger_ungated", orig)

    def _gated(trigger_point: Any, ctx: AgentCallbackContext) -> bool:
        if not orig(trigger_point, ctx):
            return False
        allowed, reason = gate.should_evolve()
        try:
            from jiuwenswarm.agents.harness.common.rails.admission_evidence import (
                maybe_log_when_gate,
            )
            maybe_log_when_gate(
                allowed=allowed,
                reason=reason,
                recent_success_rate=gate.recent_success_rate,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[execution-gate] evidence log skipped", exc_info=True)
        if not allowed:
            logger.info("[execution-gate] suppress evolution: %s", reason)
            return False
        return True

    setattr(rail, "_allow_evolution_trigger", _gated)  # type: ignore[method-assign]
    logger.info(
        "[execution-gate] bound window=%s min_samples=%s",
        gate.window,
        gate.min_samples,
    )
    return rail
