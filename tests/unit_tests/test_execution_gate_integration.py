# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-path integration tests for the execution gate and canary wiring.

These exercise the exact helpers ``build_*_rail`` call in
``evolution_rails.py`` (``_maybe_attach_execution_gate`` /
``_maybe_attach_canary``) against stub evolution rails, so the opt-in config
path is verified end to end without an LLM or network.
"""
from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.swarm.providers import evolution_rails


class _StubEvolutionRail:
    """Minimal stand-in matching the native rail's gate contract."""

    def __init__(self):
        self.written: list[str] = []

    def _allow_evolution_trigger(self, trigger_point, ctx):
        return True

    def on_skill_written(self, name, *args, **kwargs):
        self.written.append(name)
        return name


_GATE_ON = {
    "react": {"evolution": {
        "skill_evolution": True,
        "execution_gate": {"enabled": True, "window": 5, "min_samples": 2},
    }}
}
_GATE_OFF = {
    "react": {"evolution": {
        "skill_evolution": True,
        "execution_gate": {"enabled": False},
    }}
}
_CANARY_ON = {
    "react": {"evolution": {
        "skill_evolution": True,
        "canary": {"enabled": True, "core_strikes": 1, "promote_after": 2,
                   "attribution_floor": 0.5},
    }}
}
_CANARY_OFF = {
    "react": {"evolution": {
        "skill_evolution": True,
        "canary": {"enabled": False},
    }}
}


def test_execution_gate_provider_path_suppresses_converged():
    rail = _StubEvolutionRail()
    extra = evolution_rails._maybe_attach_execution_gate(rail, _GATE_ON)
    assert len(extra) == 1  # ExecutionGroundedGateRail mounted for outcomes
    ctx = SimpleNamespace()
    # zero evidence -> fail open
    assert rail._allow_evolution_trigger("after_invoke", ctx) is True
    rail._execution_gate.record_task_outcome("t0", True)
    rail._execution_gate.record_task_outcome("t1", True)
    # converged -> suppress
    assert rail._allow_evolution_trigger("after_invoke", ctx) is False


def test_execution_gate_provider_disabled_is_inert():
    rail = _StubEvolutionRail()
    extra = evolution_rails._maybe_attach_execution_gate(rail, _GATE_OFF)
    assert extra == []
    assert not hasattr(rail, "_execution_gate")
    assert rail._allow_evolution_trigger("after_invoke", None) is True


def test_canary_provider_path_admits_written_skills():
    rail = _StubEvolutionRail()
    evolution_rails._maybe_attach_canary(rail, _CANARY_ON)
    rail.on_skill_written("learned_t01")
    library = rail._canary_library
    assert "learned_t01" in library.entries
    assert library.entries["learned_t01"].tier == "probation"
    # attributed failure strikes the admitted skill (core_strikes=1 -> evict)
    dec = library.record_task(["learned_t01"], passed=False, base_rate=0.9)
    assert dec.evicted == ["learned_t01"]


def test_canary_provider_disabled_is_inert():
    rail = _StubEvolutionRail()
    evolution_rails._maybe_attach_canary(rail, _CANARY_OFF)
    assert not hasattr(rail, "_canary_library")
    rail.on_skill_written("learned_x")
    assert rail.written == ["learned_x"]  # native behaviour unchanged


def test_unknown_ctx_task_ids_are_skipped_not_poisoned():
    """The ctx recorder must not fall back to 'unknown' (permanent-suppression bug)."""
    import asyncio

    from jiuwenswarm.agents.harness.common.rails.execution_grounded_gate import (
        ExecutionGroundedGate,
        ExecutionGroundedGateRail,
    )

    gate = ExecutionGroundedGate(window=5, min_samples=2)
    rail = ExecutionGroundedGateRail(gate)
    no_id_ctx = SimpleNamespace(success=True, inputs=SimpleNamespace())
    asyncio.run(rail.after_task_iteration(no_id_ctx))
    asyncio.run(rail.after_invoke(no_id_ctx))
    assert len(gate._outcomes) == 0
    real_ctx = SimpleNamespace(success=False, inputs=SimpleNamespace(task_id="t1"))
    asyncio.run(rail.after_task_iteration(real_ctx))
    assert len(gate._outcomes) == 1
    assert gate._outcomes[0].task_id == "t1"
