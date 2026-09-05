# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for AutonomousModeRail and its hot-reload wiring.

Covers two reviewed regressions:
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain
  string — a string crashes on ``dict(content)`` at construction.
* The rail was missing from the hot-reload path, so an ``autonomy.enabled``
  change in config was silently ignored until the process restarted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.autonomous_mode_rail import (
    AutonomousModeRail,
    _SECTION_NAME,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_init_injects_section_with_dict_content() -> None:
    """The injected PromptSection carries a language-keyed mapping."""
    builder = SystemPromptBuilder(language="en")
    rail = AutonomousModeRail(enabled=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    section = builder.get_section(_SECTION_NAME)
    assert section is not None
    assert isinstance(section, PromptSection)
    assert isinstance(section.content, dict)
    assert "en" in section.content
    assert "cn" in section.content
    assert "Autonomous Execution Mode" in section.render("en")


def test_disabled_rail_injects_nothing() -> None:
    """A disabled rail leaves the prompt untouched."""
    builder = SystemPromptBuilder(language="en")
    rail = AutonomousModeRail(enabled=False)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    assert builder.get_section(_SECTION_NAME) is None


def test_before_invoke_reasserts_section() -> None:
    """A new invocation re-injects the directive without crashing."""
    builder = SystemPromptBuilder(language="en")
    rail = AutonomousModeRail(enabled=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    async def _invoke():
        await rail.before_invoke(SimpleNamespace())

    import asyncio

    asyncio.run(_invoke())

    assert builder.get_section(_SECTION_NAME) is not None


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
        "_autonomous_mode_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_rebuilt_autonomous_rail() -> None:
    """AutonomousModeRail is rebuilt from the current config on hot reload."""
    adapter = _make_adapter({"autonomy": {"enabled": True}})

    rails = adapter._get_current_agent_rails(
        {"react": {}}, {"autonomy": {"enabled": True}}
    )

    autonomous = [r for r in rails if isinstance(r, AutonomousModeRail)]
    assert len(autonomous) == 1
    assert autonomous[0]._enabled is True
    assert adapter._autonomous_mode_rail is autonomous[0]


def test_hot_reload_picks_up_autonomy_disable() -> None:
    """Disabling autonomy on reload replaces the enabled rail instance."""
    adapter = _make_adapter({"autonomy": {"enabled": True}})
    adapter._get_current_agent_rails(
        {"react": {}}, {"autonomy": {"enabled": True}}
    )
    assert adapter._autonomous_mode_rail._enabled is True

    rails = adapter._get_current_agent_rails(
        {"react": {}}, {"autonomy": {"enabled": False}}
    )

    assert adapter._autonomous_mode_rail._enabled is False
    assert not any(
        isinstance(r, AutonomousModeRail) and r._enabled
        for r in rails
    )
