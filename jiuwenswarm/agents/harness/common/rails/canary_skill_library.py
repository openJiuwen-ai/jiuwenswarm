# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Execution-conditioned canary skill library for JiuwenSwarm evolution.

Cheap when-gates (success windows) and LLM reusability scores do not
filter adversarial skill text. Harm is usually task local: a bad
SKILL.md hurts a few tasks and is a no-op on the rest. A one-shot
global admit/drop rule therefore both leaks (probe set misses the
fragile tasks) and starves (replay kills narrow but valid skills).

This module is the complementary *after* policy:

1. Candidates that pass the when-gate enter ``probation`` immediately.
   No extra LLM tokens, no content classifier.
2. After each task, ``record_task`` attributes failure only when the
   historical no-skill base rate is high enough (the task was solvable).
   Unattributed failures change nothing: no strike, and no clean-task
   credit — a skill is never promoted on the back of a failed task.
3. Probation skills are evicted on the first attributed strike; core
   skills need ``core_strikes``. Consecutive clean (successful) tasks
   promote.

Default **enabled=false**. Opt in::

    react:
      evolution:
        canary:
          enabled: true
          core_strikes: 2
          promote_after: 2
          attribution_floor: 0.5

Wiring: ``attach_canary_library`` makes ``on_skill_written`` admit every
skill the wrapped evolution rail writes (admission is automatic). Strike
and promotion recording runs through ``CanarySkillRail``: mount it as an
extra rail and give it a task-attribution source — by default it reads
``ctx.inputs.canary_skills`` (names the finished task actually used) and
the optional ``ctx.inputs.canary_base_rate`` difficulty override. Tasks
with no attribution change nothing, so mounting is always safe.

This is not a substitute for an execution-validated admit baseline
with a live pairwise judge. It is a portable, zero-token eviction
policy that wraps native evolution rails.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)


@dataclass
class CanaryEntry:
    name: str
    tier: str = "probation"
    strikes: int = 0
    clean_tasks: int = 0


@dataclass
class CanaryDecision:
    evicted: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    struck: list[str] = field(default_factory=list)
    attributed: bool = False


class CanaryLibrary:
    """Probation/core skill library with invocation-conditioned eviction."""

    def __init__(
        self,
        *,
        core_strikes: int = 2,
        promote_after: int = 2,
        attribution_floor: float = 0.5,
    ) -> None:
        if core_strikes < 1:
            raise ValueError("core_strikes must be >= 1")
        if promote_after < 1:
            raise ValueError("promote_after must be >= 1")
        if not 0.0 <= attribution_floor <= 1.0:
            raise ValueError("attribution_floor must be in [0, 1]")
        self.core_strikes = core_strikes
        self.promote_after = promote_after
        self.attribution_floor = attribution_floor
        self.entries: dict[str, CanaryEntry] = {}

    def admit(self, name: str) -> CanaryEntry:
        entry = self.entries.get(name)
        if entry is None:
            entry = CanaryEntry(name=name)
            self.entries[name] = entry
        return entry

    def active(self) -> list[str]:
        return list(self.entries.keys())

    def record_task(
        self,
        skills: Iterable[str],
        *,
        passed: bool,
        base_rate: Optional[float] = None,
    ) -> CanaryDecision:
        dec = CanaryDecision()
        names = [name for name in dict.fromkeys(skills) if name in self.entries]
        if passed:
            for entry_name in names:
                entry = self.entries[entry_name]
                entry.clean_tasks += 1
                if entry.tier == "probation" and entry.clean_tasks >= self.promote_after:
                    entry.tier = "core"
                    entry.clean_tasks = 0
                    dec.promoted.append(entry_name)
            return dec
        eligible = base_rate is None or base_rate >= self.attribution_floor
        dec.attributed = eligible
        if not eligible:
            # The task was too hard to blame on skills (base rate below the
            # floor): neither a strike nor clean-task credit. Crediting a
            # failed task toward promotion would let a skill reach core
            # without a single success.
            return dec
        for entry_name in names:
            entry = self.entries[entry_name]
            entry.strikes += 1
            entry.clean_tasks = 0
            dec.struck.append(entry_name)
            limit = 1 if entry.tier == "probation" else self.core_strikes
            if entry.strikes >= limit:
                del self.entries[entry_name]
                dec.evicted.append(entry_name)
        return dec


class CanarySkillRail(DeepAgentRail):
    """Mounted rail that feeds task outcomes into a bound ``CanaryLibrary``.

    Strike / promotion recording needs to know which library skills a
    finished task actually used. That signal is host-specific, so it is
    injected: ``skills_for_task(ctx)`` returns the attributed skill names
    (default: ``ctx.inputs.canary_skills``, an optional
    ``ctx.inputs.canary_base_rate`` carries the task's no-skill difficulty).
    Tasks with no attribution record nothing — mounting this rail without
    a source is safe and admission-only.

    Outcome recording mirrors ``ExecutionGroundedGateRail``: a task outcome
    is only counted when the context carries a boolean ``success`` and a
    non-empty ``task_id``; DeepAgent may fire both ``after_task_iteration``
    and ``after_invoke`` for one task, so consecutive same-``task_id``
    recordings are folded into a single decision.
    """

    priority = 91

    def __init__(
        self,
        library: CanaryLibrary,
        *,
        skills_for_task: Optional[Callable[[Any], list[str]]] = None,
    ) -> None:
        super().__init__()
        self.library = library
        self._skills_for_task: Callable[[Any], list[str]] = (
            skills_for_task or _default_skills_for_task
        )
        self._last_task_id = ""

    async def after_task_iteration(self, ctx: Any) -> None:  # noqa: D102
        self._record_from_ctx(ctx)

    async def after_invoke(self, ctx: Any) -> None:  # noqa: D102
        self._record_from_ctx(ctx)

    def _record_from_ctx(self, ctx: Any) -> None:
        success = getattr(ctx, "success", None)
        if not isinstance(success, bool):
            return
        inputs = getattr(ctx, "inputs", None)
        task_id = str(getattr(inputs, "task_id", "") or "").strip()
        if not task_id or task_id == self._last_task_id:
            return
        skills = [
            name
            for name in self._skills_for_task(ctx)
            if name in self.library.entries
        ]
        if not skills:
            return
        raw_rate = getattr(inputs, "canary_base_rate", None)
        base_rate = raw_rate if isinstance(raw_rate, (int, float)) else None
        self._last_task_id = task_id
        decision = self.library.record_task(skills, passed=success, base_rate=base_rate)
        if decision.evicted:
            logger.info(
                "[canary] task %s evicted %s", task_id, decision.evicted
            )


def _default_skills_for_task(ctx: Any) -> list[str]:
    """Read the attributed skill names from ``ctx.inputs.canary_skills``."""
    raw = getattr(getattr(ctx, "inputs", None), "canary_skills", None)
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(name).strip() for name in raw if str(name).strip()]
    return []


def get_canary_skill_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Read ``react.evolution.canary``; default disabled."""
    raw = ((config or {}).get("react") or {}).get("evolution") or {}
    cfg = raw.get("canary") or {}
    return {
        "enabled": cfg.get("enabled") is True,
        "core_strikes": int(cfg.get("core_strikes", 2)),
        "promote_after": int(cfg.get("promote_after", 2)),
        "attribution_floor": float(cfg.get("attribution_floor", 0.5)),
    }


def attach_canary_library(rail: Any, library: CanaryLibrary) -> Any:
    """Bind *library* onto a native evolution rail. Opt-in, in-place."""
    # 动态挂接到非本类创建的 rail 实例：用 setattr 显式表达动态绑定（G.CLS.11）。
    setattr(rail, "_canary_library", library)
    orig = getattr(rail, "on_skill_written", None)
    if callable(orig):
        def _wrapped(name, *args, **kwargs):
            out = orig(name, *args, **kwargs)
            library.admit(str(name))
            logger.info("[canary] admit %s", name)
            return out
        rail.on_skill_written = _wrapped  # type: ignore[method-assign]
    logger.info("[canary] bound core_strikes=%s promote_after=%s", library.core_strikes, library.promote_after)
    return rail
