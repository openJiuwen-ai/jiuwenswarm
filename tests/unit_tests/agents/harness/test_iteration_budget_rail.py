# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for IterationBudgetRail and its config-driven builder.

Covers two reviewed regressions:
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain
  string — a string crashes on ``dict(content)`` at construction.
* The builder must parse ``max_iterations`` leniently (``parse_int``) and
  default it to 15 to match the agent's own iteration budget, otherwise the
  warning is silently disabled under default configuration.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.iteration_budget_rail import (
    IterationBudgetRail,
    _SECTION_NAME,
)


class _FakeSession:
    def __init__(self, iteration) -> None:
        self._iteration = iteration

    def get_state(self, key: str):
        if key == "iteration":
            return self._iteration
        return None


def _run(coro):
    return asyncio.run(coro)


def test_injects_warning_section_with_dict_content() -> None:
    """The injected PromptSection carries a language-keyed mapping."""
    builder = SystemPromptBuilder(language="en")
    rail = IterationBudgetRail(max_iterations=15, warning_threshold=10)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = SimpleNamespace(session=_FakeSession(12))  # remaining 3 <= 10

    _run(rail.before_model_call(ctx))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "Iteration budget notice" in section.render("en")


def test_no_warning_when_iterations_not_short() -> None:
    """Remaining iterations above the threshold inject nothing."""
    builder = SystemPromptBuilder(language="en")
    rail = IterationBudgetRail(max_iterations=15, warning_threshold=10)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = SimpleNamespace(session=_FakeSession(2))  # remaining 13 > 10

    _run(rail.before_model_call(ctx))

    assert builder.get_section(_SECTION_NAME) is None


def test_before_invoke_clears_stale_warning() -> None:
    """A new invocation removes any warning from the previous round."""
    builder = SystemPromptBuilder(language="en")
    rail = IterationBudgetRail(max_iterations=15, warning_threshold=10)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = SimpleNamespace(session=_FakeSession(12))
    _run(rail.before_model_call(ctx))
    assert builder.get_section(_SECTION_NAME) is not None

    _run(rail.before_invoke(ctx))

    assert builder.get_section(_SECTION_NAME) is None


def test_build_rail_defaults_to_agent_iteration_budget() -> None:
    """Empty config falls back to 15 iterations, matching the agent default."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = JiuWenSwarmDeepAdapter._build_iteration_budget_rail({})

    assert rail is not None
    assert rail._max_iterations == 15
    assert rail._warning_threshold == 10


def test_build_rail_tolerates_null_and_empty_values() -> None:
    """Null/empty config values fall back instead of crashing int()."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = JiuWenSwarmDeepAdapter._build_iteration_budget_rail(
        {"max_iterations": None, "budget_warning_threshold": ""}
    )

    assert rail is not None
    assert rail._max_iterations == 15
    assert rail._warning_threshold == 10


def test_build_rail_honors_explicit_values() -> None:
    """Explicit non-null values are honored."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = JiuWenSwarmDeepAdapter._build_iteration_budget_rail(
        {"max_iterations": 200, "budget_warning_threshold": 5}
    )

    assert rail is not None
    assert rail._max_iterations == 200
    assert rail._warning_threshold == 5


def test_rail_never_raises_on_bad_config() -> None:
    """A non-integer value must yield None, not a crash."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = JiuWenSwarmDeepAdapter._build_iteration_budget_rail(
        {"max_iterations": "many", "budget_warning_threshold": "soon"}
    )

    assert rail is not None
    assert rail._max_iterations == 15
    assert rail._warning_threshold == 10
