# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the jiuwenswarm wiring of agent-core's BudgetNoticeRail.

The rail itself (reading the loop's budgets, thresholds, and section
rendering) is unit-tested in agent-core. These tests cover the host adapter
builder that maps jiuwenswarm config onto the rail — in particular that the
rail reads the loop's real budget instead of a duplicated host copy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import BudgetNoticeRail
from openjiuwen.harness.schema.stop_condition import MaxRoundsEvaluator
from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _run(coro):
    return asyncio.run(coro)


def _build(config: dict):
    return JiuWenSwarmDeepAdapter._build_budget_notice_rail(config)


def test_builder_defaults_round_threshold() -> None:
    """Empty config falls back to 10 remaining rounds (the previous default)."""
    rail = _build({})
    assert isinstance(rail, BudgetNoticeRail)
    assert rail._round_remaining == 10


def test_builder_tolerates_null_and_empty_values() -> None:
    """Null / empty threshold falls back instead of crashing."""
    assert _build({"budget_warning_threshold": None})._round_remaining == 10
    assert _build({"budget_warning_threshold": ""})._round_remaining == 10


def test_builder_honors_explicit_threshold() -> None:
    assert _build({"budget_warning_threshold": 5})._round_remaining == 5


def test_builder_never_raises_on_bad_value() -> None:
    """A non-integer value falls back to the default, not a crash."""
    rail = _build({"budget_warning_threshold": "soon"})
    assert rail is not None
    assert rail._round_remaining == 10


def test_rail_reads_loop_budget_not_host_copy() -> None:
    """Warnings track the loop's real rounds limit, not a host-side number.

    ``max_iterations`` is deliberately absent from the host config here: the
    loop enforces 50 rounds and the rail must warn based on that value.
    """
    rail = _build({"budget_warning_threshold": 3})
    builder = SystemPromptBuilder(language="en")
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    coordinator = LoopCoordinator([MaxRoundsEvaluator(50)])
    coordinator.reset()
    for _ in range(48):  # 2 rounds left <= 3
        coordinator.increment_iteration()
    ctx = SimpleNamespace(agent=SimpleNamespace(loop_coordinator=coordinator))

    _run(rail.before_model_call(ctx))
    assert builder.get_section(SectionName.BUDGET_NOTICE) is not None

    # A healthy budget clears the notice.
    healthy = LoopCoordinator([MaxRoundsEvaluator(50)])
    healthy.reset()
    healthy.increment_iteration()  # 49 left
    _run(rail.before_model_call(SimpleNamespace(agent=SimpleNamespace(loop_coordinator=healthy))))
    assert builder.get_section(SectionName.BUDGET_NOTICE) is None
