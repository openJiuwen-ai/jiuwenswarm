# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the execution-grounded Skill-evolution gate (no LLM, no network)."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.execution_grounded_gate import (
    ExecutionGroundedGate,
    attach_execution_gate,
)


def test_insufficient_samples_blocks():
    g = ExecutionGroundedGate(window=5, min_samples=3)
    g.record_task_outcome("t0", True)
    g.record_task_outcome("t1", False)
    allowed, reason = g.should_evolve()
    assert allowed is False
    assert "insufficient" in reason


def test_converged_window_blocks():
    g = ExecutionGroundedGate(window=5, min_samples=3)
    for i in range(5):
        g.record_task_outcome(f"t{i}", True)
    allowed, reason = g.should_evolve()
    assert allowed is False
    assert "converged" in reason


def test_headroom_allows():
    g = ExecutionGroundedGate(window=5, min_samples=3)
    g.record_task_outcome("t0", True)
    g.record_task_outcome("t1", False)
    g.record_task_outcome("t2", True)
    allowed, reason = g.should_evolve()
    assert allowed is True
    assert "improvable" in reason


def test_empty_window_fails_open():
    """Zero evidence must not silently disable the feature being guarded."""
    g = ExecutionGroundedGate(window=5, min_samples=3)
    allowed, reason = g.should_evolve()
    assert allowed is True
    assert "fail open" in reason


def test_same_task_id_does_not_double_count():
    g = ExecutionGroundedGate(window=5, min_samples=3)
    g.record_task_outcome("t0", True)
    g.record_task_outcome("t0", True)  # after_invoke duplicate
    g.record_task_outcome("t1", False)
    g.record_task_outcome("t2", True)
    assert len(g._outcomes) == 3
    allowed, _ = g.should_evolve()
    assert allowed is True


def test_empty_task_id_is_skipped_not_poisoned():
    """Blank ids cannot be de-duplicated; recording them kept the window under
    min_samples forever (permanently suppressing evolution)."""
    g = ExecutionGroundedGate(window=5, min_samples=2)
    g.record_task_outcome("", True)
    g.record_task_outcome("   ", False)
    g.record_task_outcome(None, True)
    assert len(g._outcomes) == 0
    g.record_task_outcome("t0", True)
    g.record_task_outcome("t1", False)
    allowed, _ = g.should_evolve()
    assert allowed is True


def test_min_samples_must_be_positive():
    with pytest.raises(ValueError):
        ExecutionGroundedGate(window=5, min_samples=0)


def test_attach_wraps_allow_evolution_trigger():
    class _Rail:
        def _allow_evolution_trigger(self, trigger_point, ctx):
            return True

    rail = _Rail()
    g = ExecutionGroundedGate(window=5, min_samples=2)
    attach_execution_gate(rail, g)
    ctx = SimpleNamespace()
    # zero evidence -> fail open even though orig returns True
    assert rail._allow_evolution_trigger("after_invoke", ctx) is True
    g.record_task_outcome("a", True)
    # one outcome, window below min_samples -> warm-up suppression
    assert rail._allow_evolution_trigger("after_invoke", ctx) is False
    g.record_task_outcome("b", False)
    assert rail._allow_evolution_trigger("after_invoke", ctx) is True
    # fill the window with successes -> converged -> suppress
    for i in range(5):
        g.record_task_outcome(f"c{i}", True)
    assert rail._allow_evolution_trigger("after_invoke", ctx) is False
