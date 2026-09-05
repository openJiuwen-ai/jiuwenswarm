# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for FailureMemoryRail and its hot-reload wiring.

Covers the reviewed regressions plus the recurring rail-integration classes:
* Non-string ``tool_result`` (list / int / dict) must be coerced to ``str``
  before error-pattern matching, not passed through as-is.
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain string.
* The rail was missing from the hot-reload path, so ``failure_memory`` config
  changes were silently ignored until a cold restart.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.failure_memory_rail import (
    FailureMemoryRail,
    _SECTION_NAME,
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


def _make_rail(max_failures: int = 10) -> FailureMemoryRail:
    builder = SystemPromptBuilder(language="en")
    rail = FailureMemoryRail(max_failures=max_failures)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    return rail, builder


def _ctx(session, tool_result, tool_name: str = "read_file", tool_args=None):
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=tool_args or {"path": "a.txt"},
            tool_result=tool_result,
        ),
        session=session,
        exception=None,
    )


def test_non_string_error_result_is_coerced(tmp_path) -> None:
    """A list tool_result containing an error is coerced and recorded."""
    rail, builder = _make_rail()
    session = _FakeSession()

    _run(rail.after_tool_call(_ctx(session, ["line1", "Error: something"])))

    failures = session.get_state("_failure_memory")
    assert failures is not None
    assert len(failures) == 1
    assert failures[0]["tool"] == "read_file"
    assert "Error: something" in failures[0]["error"]

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "Error: something" in section.render("en")


def test_success_result_is_ignored(tmp_path) -> None:
    """A non-error result (even non-string) records nothing."""
    rail, builder = _make_rail()
    session = _FakeSession()

    _run(rail.after_tool_call(_ctx(session, ["line1", "all good"])))

    assert session.get_state("_failure_memory") is None
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
        "_failure_memory_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_failure_memory_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {"failure_memory": {"enabled": True, "max_failures": 20}}

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    fm = [r for r in rails if isinstance(r, FailureMemoryRail)]
    assert len(fm) == 1
    assert fm[0]._max_failures == 20
    assert adapter._failure_memory_rail is fm[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"failure_memory": {"enabled": True, "max_failures": 10}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, FailureMemoryRail))

    config_base_off = {"failure_memory": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._failure_memory_rail is None


def test_build_rail_tolerates_null_max_failures() -> None:
    """A null max_failures falls back to 10 instead of crashing int()."""
    rail = JiuWenSwarmDeepAdapter._build_failure_memory_rail(
        {"failure_memory": {"enabled": True, "max_failures": None}}
    )

    assert rail is not None
    assert rail._max_failures == 10
