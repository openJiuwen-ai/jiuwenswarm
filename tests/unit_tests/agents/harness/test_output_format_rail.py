# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for OutputFormatRail and its hot-reload wiring.

Covers the reviewed regressions plus the recurring rail-integration classes:
* CRLF task files must parse the same as LF files (frontmatter / paragraphs /
  fenced code blocks).
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain string.
* The rail was missing from the hot-reload path, so ``output_format`` config
  changes were silently ignored until a cold restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.output_format_rail import (
    OutputFormatRail,
    _SECTION_NAME,
    _has_format_signal,
    _strip_frontmatter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _run(coro):
    return asyncio.run(coro)


def test_crlf_task_file_is_parsed(tmp_path: Path) -> None:
    """A Windows CRLF task file injects the same reminder as an LF file."""
    task_file = tmp_path / "task.md"
    task_file.write_bytes(
        b"---\r\ntitle: demo\r\n---\r\n\r\n"
        b"Describe what to build.\r\n\r\n"
        b"The output must be JSON:\r\n\r\n"
        b"```json\r\n{\"ok\": true}\r\n```\r\n"
    )
    builder = SystemPromptBuilder(language="en")
    rail = OutputFormatRail(task_path=str(task_file), max_chars=800)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    _run(rail.before_invoke(SimpleNamespace()))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    rendered = section.render("en")
    assert "Required Output Format" in rendered
    assert "The output must be JSON" in rendered


def test_strip_frontmatter_handles_crlf() -> None:
    body = _strip_frontmatter("---\r\ntitle: x\r\n---\r\n\r\ncontent here\r\n")
    assert "---" not in body
    assert "content here" in body


def test_format_signal_is_specific() -> None:
    """The narrowed regex still catches format terms and not generic words."""
    assert _has_format_signal("The output must be JSON")
    assert _has_format_signal("save the report as csv")
    assert not _has_format_signal("please write the file to disk")


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
        "_output_format_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_output_format_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {
        "output_format": {"enabled": True, "path": "/app/task.md", "max_chars": 1200}
    }

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    of = [r for r in rails if isinstance(r, OutputFormatRail)]
    assert len(of) == 1
    assert of[0]._max_chars == 1200
    assert adapter._output_format_rail is of[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"output_format": {"enabled": True, "path": "/app/task.md"}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, OutputFormatRail))

    config_base_off = {"output_format": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._output_format_rail is None


def test_build_rail_tolerates_null_max_chars() -> None:
    """A null max_chars falls back to 800 instead of crashing int()."""
    rail = JiuWenSwarmDeepAdapter._build_output_format_rail(
        {"output_format": {"enabled": True, "max_chars": None}}
    )

    assert rail is not None
    assert rail._max_chars == 800
