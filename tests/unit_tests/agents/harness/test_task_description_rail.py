# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for TaskDescriptionRail and its hot-reload wiring.

Covers the reviewed regressions:
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain
  string.
* The rail was missing from the hot-reload path, so it was silently lost
  (and its section removed) after a config-triggered reconfiguration.
* An empty task file must not be treated as injected, so the rail keeps
  retrying until the file is populated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.task_description_rail import (
    TaskDescriptionRail,
    _SECTION_NAME,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _run(coro):
    return asyncio.run(coro)


def _make_rail(builder: SystemPromptBuilder, task_path: str) -> TaskDescriptionRail:
    rail = TaskDescriptionRail(task_path)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    return rail


def test_injects_section_with_dict_content(tmp_path: Path) -> None:
    """The injected PromptSection carries a language-keyed mapping."""
    task_file = tmp_path / "task.md"
    task_file.write_text("Build the thing.", encoding="utf-8")
    builder = SystemPromptBuilder(language="en")
    rail = _make_rail(builder, str(task_file))

    _run(rail.before_invoke(SimpleNamespace()))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "Build the thing." in section.render("en")


def test_empty_file_is_not_marked_injected(tmp_path: Path) -> None:
    """An empty task file injects nothing and is retried on the next model call."""
    task_file = tmp_path / "task.md"
    task_file.write_text("   ", encoding="utf-8")
    builder = SystemPromptBuilder(language="en")
    rail = _make_rail(builder, str(task_file))

    _run(rail.before_invoke(SimpleNamespace()))

    assert builder.get_section(_SECTION_NAME) is None
    assert rail._injected is False

    # Populate the file; the next model call must now inject it.
    task_file.write_text("Now there is a task.", encoding="utf-8")
    _run(rail.before_model_call(SimpleNamespace()))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert "Now there is a task." in section.render("en")


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
        "_task_description_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_task_description_rail(tmp_path: Path) -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    task_file = tmp_path / "task.md"
    task_file.write_text("task", encoding="utf-8")
    adapter = _make_adapter({})
    config_base = {
        "task_description": {"enabled": True, "path": str(task_file)}
    }

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    td = [r for r in rails if isinstance(r, TaskDescriptionRail)]
    assert len(td) == 1
    assert td[0]._task_path == str(task_file)
    assert adapter._task_description_rail is td[0]


def test_hot_reload_disable_tears_down_rail(tmp_path: Path) -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    task_file = tmp_path / "task.md"
    task_file.write_text("task", encoding="utf-8")
    adapter = _make_adapter({})
    config_base_on = {
        "task_description": {"enabled": True, "path": str(task_file)}
    }
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, TaskDescriptionRail))

    config_base_off = {"task_description": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    # The old instance is still listed (so _hot_reload_rails uninit's it).
    assert old_rail in rails
    assert adapter._task_description_rail is None
