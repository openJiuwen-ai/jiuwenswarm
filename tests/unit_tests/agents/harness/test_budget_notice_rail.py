# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the jiuwenswarm wiring of agent-core's budget rail.

The rail itself (reading the loop's budgets, thresholds, and section
rendering) is unit-tested in agent-core. These tests cover the host adapter
builders that map jiuwenswarm config onto the two rails:

* ``_build_budget_notice_rail`` — thresholds only, never a duplicated loop
  limit.
* ``_build_task_completion_rail`` — rounds are default-on, token / wall-clock
  caps are opt-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import BudgetNoticeRail, TaskCompletionRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.schema.stop_condition import (
    BudgetLimit,
    MaxRoundsEvaluator,
)
from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _run(coro):
    return asyncio.run(coro)


def _build(config: dict) -> BudgetNoticeRail:
    return JiuWenSwarmDeepAdapter._build_budget_notice_rail(config)


def _build_task_rail(config: dict) -> TaskCompletionRail:
    return JiuWenSwarmDeepAdapter._build_task_completion_rail(config)


def test_builder_defaults_to_round_ratio() -> None:
    """Empty config warns by ratio (20%), not a fixed number of rounds."""
    rail = _build({})
    assert isinstance(rail, BudgetNoticeRail)
    assert rail._round_remaining is None
    assert rail._round_ratio == 0.20
    assert rail._token_ratio == 0.15
    assert rail._time_ratio == 0.15


def test_builder_honors_ratio_and_token_time_overrides() -> None:
    rail = _build(
        {
            "budget_warning_ratio": 0.5,
            "budget_warning_token_ratio": 0.25,
            "budget_warning_time_ratio": 0.1,
        }
    )
    assert rail._round_ratio == 0.5
    assert rail._token_ratio == 0.25
    assert rail._time_ratio == 0.1


def test_builder_absolute_threshold_overrides_ratio() -> None:
    rail = _build({"budget_warning_threshold": 5, "budget_warning_ratio": 0.5})
    assert rail._round_remaining == 5


def test_builder_tolerates_null_and_empty_values() -> None:
    """Null / empty threshold means "unset", so the ratio default applies."""
    assert _build({"budget_warning_threshold": None})._round_remaining is None
    assert _build({"budget_warning_threshold": ""})._round_remaining is None


def test_builder_never_raises_on_bad_values() -> None:
    """Non-numeric values fall back to the defaults, not a crash."""
    rail = _build({"budget_warning_threshold": "soon", "budget_warning_ratio": "later"})
    assert rail is not None
    assert rail._round_remaining is None
    assert rail._round_ratio == 0.20


def test_task_rail_defaults_rounds_on_and_token_time_off() -> None:
    """Rounds are wired; token / wall-clock caps stay unset (opt-in)."""
    rail = _build_task_rail({"max_iterations": 100})
    assert rail.max_rounds == 100
    assert rail.max_tokens is None
    assert rail.timeout_seconds is None
    assert [e.budget() for e in rail.build_evaluators()] == [
        BudgetLimit("rounds", 100.0)
    ]


def test_task_rail_wires_opt_in_token_and_timeout() -> None:
    """Explicit ``max_tokens`` / ``timeout_seconds`` become real budgets."""
    rail = _build_task_rail(
        {"max_iterations": 30, "max_tokens": 1000, "timeout_seconds": 60}
    )
    assert rail.max_rounds == 30
    assert rail.max_tokens == 1000
    assert rail.timeout_seconds == 60
    assert [e.budget() for e in rail.build_evaluators()] == [
        BudgetLimit("rounds", 30.0),
        BudgetLimit("seconds", 60.0),
        BudgetLimit("tokens", 1000.0),
    ]


def test_task_rail_falls_back_to_15_rounds() -> None:
    rail = _build_task_rail({})
    assert rail.max_rounds == 15


def _subagent_spec(
    *, enable_task_loop: bool, max_iterations: int | None = None, rails=None
) -> SubAgentConfig:
    return SubAgentConfig(
        agent_card=AgentCard(name="custom", id="subagent_custom"),
        system_prompt="do work",
        enable_task_loop=enable_task_loop,
        max_iterations=max_iterations,
        rails=rails,
    )


def test_subagent_budget_rails_added_for_task_loop_specs() -> None:
    spec = _subagent_spec(enable_task_loop=True, max_iterations=20)
    JiuWenSwarmDeepAdapter._ensure_subagent_budget_rails(
        [spec], {"max_iterations": 100}
    )
    rails = spec.rails or []
    task_rails = [r for r in rails if isinstance(r, TaskCompletionRail)]
    assert len(task_rails) == 1
    assert task_rails[0].max_rounds == 20
    assert any(isinstance(r, BudgetNoticeRail) for r in rails)


def test_subagent_budget_rails_skipped_without_task_loop() -> None:
    spec = _subagent_spec(enable_task_loop=False, max_iterations=20)
    JiuWenSwarmDeepAdapter._ensure_subagent_budget_rails([spec], {})
    assert not spec.rails


def test_subagent_budget_rails_not_duplicated() -> None:
    existing_task = TaskCompletionRail(max_rounds=7)
    existing_notice = BudgetNoticeRail(round_ratio=0.3)
    spec = _subagent_spec(
        enable_task_loop=True,
        max_iterations=20,
        rails=[existing_task, existing_notice],
    )
    JiuWenSwarmDeepAdapter._ensure_subagent_budget_rails([spec], {})
    assert spec.rails.count(existing_task) == 1
    assert spec.rails.count(existing_notice) == 1
    assert len([r for r in spec.rails if isinstance(r, TaskCompletionRail)]) == 1


def test_subagent_budget_rails_use_parent_rounds_when_unset() -> None:
    spec = _subagent_spec(enable_task_loop=True, max_iterations=None)
    JiuWenSwarmDeepAdapter._ensure_subagent_budget_rails(
        [spec], {"max_iterations": 42}
    )
    task_rails = [r for r in (spec.rails or []) if isinstance(r, TaskCompletionRail)]
    assert task_rails and task_rails[0].max_rounds == 42


def test_rail_reads_loop_budget_not_host_copy() -> None:
    """Warnings track the loop's real rounds limit, not a host-side number.

    No threshold or ``max_iterations`` is supplied, so the rail must derive the
    warning from the loop's own budget (default 20% of 50 rounds).
    """
    rail = _build({})
    builder = SystemPromptBuilder(language="en")
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    coordinator = LoopCoordinator([MaxRoundsEvaluator(50)])
    coordinator.reset()
    for _ in range(48):  # 2 rounds left = 4% <= 20%
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
