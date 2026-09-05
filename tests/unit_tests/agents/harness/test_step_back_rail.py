# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for StepBackRail and its hot-reload wiring.

Covers the reviewed regressions plus the recurring rail-integration classes:
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain string.
* ``_parse_exit_code`` must not treat a JSON boolean as an exit code — a
  ``false`` would fake exit code 0 and reset the failure counter.
* Zero/negative ``step_back_after`` must not inject the hint on every call.
* The rail was missing from the hot-reload path.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.step_back_rail import (
    StepBackRail,
    _SECTION_NAME,
    _parse_exit_code,
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


def test_injects_section_with_dict_content() -> None:
    """The step-back PromptSection carries a language-keyed mapping."""
    builder = SystemPromptBuilder(language="en")
    rail = StepBackRail(step_back_after=2)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    session = _FakeSession()

    _run(rail.after_tool_call(_ctx(session, json.dumps({"exit_code": 1}))))
    _run(rail.after_tool_call(_ctx(session, json.dumps({"exit_code": 1}))))
    _run(rail.before_model_call(_ctx(session, None)))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "2 consecutive shell failures" in section.render("en")


def test_parse_exit_code_rejects_boolean() -> None:
    """A JSON boolean exit code is ignored, not coerced to 0."""
    assert _parse_exit_code(json.dumps({"exit_code": True})) is None
    assert _parse_exit_code(json.dumps({"exit_code": False})) is None
    assert _parse_exit_code(json.dumps({"exit_code": 1})) == 1
    assert _parse_exit_code(json.dumps({"exit_code": "0"})) == 0


def test_success_resets_counter() -> None:
    """Exit code 0 resets the consecutive-failure counter."""
    rail = StepBackRail()
    session = _FakeSession()
    _run(rail.after_tool_call(_ctx(session, json.dumps({"exit_code": 1}))))
    _run(rail.after_tool_call(_ctx(session, json.dumps({"exit_code": 0}))))

    assert session.get_state("_step_back_consecutive_failures") == 0


def test_zero_threshold_does_not_inject_every_call() -> None:
    """step_back_after <= 0 is clamped to 1, not injecting unconditionally."""
    builder = SystemPromptBuilder(language="en")
    rail = StepBackRail(step_back_after=0)
    assert rail._step_back_after == 1
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    session = _FakeSession()

    _run(rail.before_model_call(_ctx(session, None)))

    assert builder.get_section(_SECTION_NAME) is None


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
        "_step_back_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_step_back_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {"step_back": {"enabled": True, "step_back_after": 5}}

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    sb = [r for r in rails if isinstance(r, StepBackRail)]
    assert len(sb) == 1
    assert sb[0]._step_back_after == 5
    assert adapter._step_back_rail is sb[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"step_back": {"enabled": True, "step_back_after": 3}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, StepBackRail))

    config_base_off = {"step_back": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._step_back_rail is None


def test_build_rail_tolerates_null_and_zero_threshold() -> None:
    """Null step_back_after falls back to 3; zero is clamped to 1."""
    rail = JiuWenSwarmDeepAdapter._build_step_back_rail(
        {"step_back": {"enabled": True, "step_back_after": None}}
    )

    assert rail is not None
    assert rail._step_back_after == 3

    rail_zero = JiuWenSwarmDeepAdapter._build_step_back_rail(
        {"step_back": {"enabled": True, "step_back_after": 0}}
    )

    assert rail_zero is not None
    assert rail_zero._step_back_after == 1
