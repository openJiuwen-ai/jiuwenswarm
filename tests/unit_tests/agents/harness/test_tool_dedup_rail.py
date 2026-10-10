# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ToolCallDeduplicationRail and its hot-reload wiring.

Covers the reviewed regressions:
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain
  string — the cross-turn warning crashed on ``dict(content)``.
* The rail was missing from the hot-reload path, so ``tool_dedup`` config
  changes were silently ignored until a cold restart.
* ``_inject_warning`` took three parameters it never used, and the rail kept
  a ``_current_hit_key`` attribute that nothing read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.tool_dedup_rail import (
    ToolCallDeduplicationRail,
    _SECTION_NAME,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.permission_runtime_state import (
    SessionPermissionState,
)


def _run(coro):
    return asyncio.run(coro)


def _tool_ctx():
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name="read_file",
            tool_args={"path": "a.txt"},
            tool_result="file content",
            tool_call=SimpleNamespace(id="t1"),
        ),
        extra={},
    )


def test_warning_section_uses_dict_content() -> None:
    """The cross-turn warning PromptSection carries a language-keyed mapping."""
    builder = SystemPromptBuilder(language="en")
    rail = ToolCallDeduplicationRail(warn_after=3)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    for _ in range(3):
        ctx = _tool_ctx()
        _run(rail.after_tool_call(ctx))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "Repeated tool-call notice" in section.render("en")


def test_duplicate_within_turn_is_suppressed() -> None:
    """A repeated call in the same turn returns the cached result, no re-run."""
    rail = ToolCallDeduplicationRail()
    first = _tool_ctx()
    _run(rail.after_tool_call(first))  # caches the real result

    second = _tool_ctx()
    _run(rail.before_tool_call(second))

    assert second.extra.get("_skip_tool") is True
    assert second.extra.get("_dedup_hit") is True
    assert second.inputs.tool_result == "file content"


def test_side_effecting_tool_invalidates_turn_cache() -> None:
    """A write between two identical reads must drop the cached read result.

    read("a.txt") caches "file not found"; write("a.txt") changes the file;
    the second read must run for real instead of being served the stale cache.
    """
    rail = ToolCallDeduplicationRail()

    first_read = _tool_ctx()
    first_read.inputs.tool_result = "file not found"
    _run(rail.after_tool_call(first_read))

    write = _tool_ctx()
    write.inputs.tool_name = "write_file"
    write.inputs.tool_args = {"path": "a.txt", "content": "content"}
    write.inputs.tool_result = "ok"
    _run(rail.before_tool_call(write))
    _run(rail.after_tool_call(write))

    second_read = _tool_ctx()
    _run(rail.before_tool_call(second_read))

    assert second_read.extra.get("_skip_tool") is None
    assert second_read.extra.get("_dedup_hit") is None


def test_side_effecting_before_call_invalidates_without_after() -> None:
    """The before-hook alone must invalidate, in case after_tool_call is skipped."""
    rail = ToolCallDeduplicationRail()

    first_read = _tool_ctx()
    first_read.inputs.tool_result = "file not found"
    _run(rail.after_tool_call(first_read))

    write = _tool_ctx()
    write.inputs.tool_name = "write_file"
    write.inputs.tool_args = {"path": "a.txt", "content": "content"}
    _run(rail.before_tool_call(write))  # deliberately no after_tool_call

    second_read = _tool_ctx()
    _run(rail.before_tool_call(second_read))

    assert second_read.extra.get("_skip_tool") is None


def test_warn_after_is_floored_to_one() -> None:
    """A zero/negative threshold must not warn on the very first execution."""
    assert ToolCallDeduplicationRail(0)._warn_after == 1
    assert ToolCallDeduplicationRail(-5)._warn_after == 1
    assert ToolCallDeduplicationRail(None)._warn_after == 3


def _make_adapter(config_base: dict) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = config_base
    adapter._config_cache = {"react": {}}
    adapter._instance_overrides = {}
    adapter._filesystem_rail = None
    adapter._heartbeat_service = None
    adapter._skill_manager = None
    adapter._is_cron_execution = False
    adapter._parent_session_id = None
    adapter._permission_state = SessionPermissionState()
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
        "_tool_call_deduplication_rail",
        "_ttse_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_dedup_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {"tool_dedup": {"enabled": True, "warn_after": 5}}

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    dedup = [r for r in rails if isinstance(r, ToolCallDeduplicationRail)]
    assert len(dedup) == 1
    assert dedup[0]._warn_after == 5
    assert adapter._tool_call_deduplication_rail is dedup[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"tool_dedup": {"enabled": True, "warn_after": 3}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, ToolCallDeduplicationRail))

    config_base_off = {"tool_dedup": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._tool_call_deduplication_rail is None


def test_build_rail_tolerates_null_warn_after() -> None:
    """A null warn_after falls back to 3 instead of crashing int()."""
    rail = JiuWenSwarmDeepAdapter._build_tool_call_deduplication_rail(
        {"tool_dedup": {"enabled": True, "warn_after": None}}
    )

    assert rail is not None
    assert rail._warn_after == 3
