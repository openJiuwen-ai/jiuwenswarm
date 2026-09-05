# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for VerifierCircuitBreakerRail and its hot-reload wiring.

Covers the reviewed regressions plus the recurring rail-integration classes:
* ``_VERIFIER_MARKERS`` must not let a single "passed" / "failed" hit two
  markers and pass the ``hits >= 2`` verifier-output threshold.
* Reward matching must accept exactly 1 (or 1.0 / 1.00), never 10 / 100 / 1.5.
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain string.
* The rail was missing from the hot-reload path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.verifier_circuit_breaker_rail import (
    VerifierCircuitBreakerRail,
    _SECTION_NAME,
    _is_verifier_output,
    _is_verifier_success,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeSession:
    def __init__(self) -> None:
        self._state: dict = {}

    def get_state(self, key: str):
        return self._state.get(key)

    def update_state(self, mapping: dict) -> None:
        self._state.update(mapping)


def _run(coro):
    return asyncio.run(coro)


def _ctx(session, tool_result, tool_name: str = "bash"):
    return SimpleNamespace(
        inputs=SimpleNamespace(tool_name=tool_name, tool_args={}, tool_result=tool_result),
        session=session,
        exception=None,
    )


def test_single_passed_or_failed_is_not_verifier_output() -> None:
    """One "passed"/"failed" occurrence alone must not reach the threshold."""
    assert not _is_verifier_output("all tests passed")
    assert not _is_verifier_output("one test failed")
    # Two distinct markers are still needed.
    assert _is_verifier_output("pytest: 1 passed")


def test_reward_matching_is_precise() -> None:
    """Only reward exactly 1 (or 1.0 / 1.00) counts as success."""
    assert _is_verifier_success("reward: 1")
    assert _is_verifier_success("reward: 1.0")
    assert _is_verifier_success('"reward": 1')
    assert _is_verifier_success('"reward": 1.00')
    assert not _is_verifier_success("reward: 10")
    assert not _is_verifier_success("reward: 100")
    assert not _is_verifier_success("reward: 1.5")
    assert not _is_verifier_success('"reward": 10')


def test_injects_section_with_dict_content() -> None:
    """The circuit-breaker PromptSection carries a language-keyed mapping."""
    builder = SystemPromptBuilder(language="en")
    rail = VerifierCircuitBreakerRail(break_after=2)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    session = _FakeSession()

    verifier_out = "pytest: 1 failed\nAssertionError: boom"
    _run(rail.after_tool_call(_ctx(session, verifier_out)))
    _run(rail.after_tool_call(_ctx(session, verifier_out)))
    _run(rail.before_model_call(_ctx(session, None)))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "CIRCUIT BREAKER" in section.render("en")


def _make_adapter(config_base: dict) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = config_base
    adapter._config_cache = {"react": {}}
    adapter._instance_overrides = {}
    adapter._filesystem_rail = None
    adapter._heartbeat_service = None
    adapter._skill_manager = None
    for attr in (
        "_skill_evolution_rail",
        "_skill_rail",
        "_context_assemble_rail",
        "_context_processor_rail",
        "_memory_rail",
        "_avatar_rail",
        "_memory_forbidden_rail",
        "_permission_rail",
        "_heartbeat_rail",
        "_verifier_circuit_breaker_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_verifier_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {"verifier_circuit_breaker": {"enabled": True, "break_after": 5}}

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    vcb = [r for r in rails if isinstance(r, VerifierCircuitBreakerRail)]
    assert len(vcb) == 1
    assert vcb[0]._break_after == 5
    assert adapter._verifier_circuit_breaker_rail is vcb[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"verifier_circuit_breaker": {"enabled": True, "break_after": 3}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, VerifierCircuitBreakerRail))

    config_base_off = {"verifier_circuit_breaker": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._verifier_circuit_breaker_rail is None


def test_build_rail_tolerates_null_break_after() -> None:
    """A null break_after falls back to 3 instead of crashing int()."""
    rail = JiuWenSwarmDeepAdapter._build_verifier_circuit_breaker_rail(
        {"verifier_circuit_breaker": {"enabled": True, "break_after": None}}
    )

    assert rail is not None
    assert rail._break_after == 3
