# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextHeadroomRail and its hot-reload wiring.

Covers the reviewed regressions plus the recurring rail-integration classes:
* Reversed warn_ratio / critical_ratio thresholds must not make the warn
  branch unreachable.
* ``_usage_ratio`` must not silently replace a 0 context window with the
  fallback (the <= 0 guard must fire).
* ``PromptSection`` requires a ``{language: text}`` mapping, not a plain string.
* The rail was missing from the hot-reload path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.context_headroom_rail import (
    ContextHeadroomRail,
    _SECTION_NAME,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _run(coro):
    return asyncio.run(coro)


def _ctx(total_tokens: int, window_tokens=None):
    return SimpleNamespace(
        context=SimpleNamespace(
            statistic=lambda: SimpleNamespace(total_tokens=total_tokens),
            _context_window_tokens=window_tokens,
        )
    )


def test_reversed_thresholds_are_swapped() -> None:
    """warn_ratio >= critical_ratio is corrected so warn stays reachable."""
    rail = ContextHeadroomRail(warn_ratio=0.80, critical_ratio=0.60)
    assert rail._warn_ratio == 0.60
    assert rail._critical_ratio == 0.80


def test_warn_and_critical_sections_use_dict_content() -> None:
    """The injected PromptSections carry language-keyed mappings."""
    builder = SystemPromptBuilder(language="en")
    rail = ContextHeadroomRail(warn_ratio=0.60, critical_ratio=0.80)
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    _run(rail.before_model_call(_ctx(total_tokens=65, window_tokens=100)))
    warn = builder.get_section(_SECTION_NAME)
    assert warn is not None
    assert isinstance(warn, PromptSection)
    assert "en" in warn.content and "cn" in warn.content
    assert "Context window filling up" in warn.render("en")

    _run(rail.before_model_call(_ctx(total_tokens=85, window_tokens=100)))
    critical = builder.get_section(_SECTION_NAME)
    assert critical is not None
    assert "CRITICAL: Context window is nearly full" in critical.render("en")


def test_zero_context_window_is_not_replaced() -> None:
    """A 0 window must trip the guard, not fall back to the default."""
    rail = ContextHeadroomRail()
    ratio = rail._usage_ratio(_ctx(total_tokens=10, window_tokens=0))
    assert ratio is None


def test_none_window_falls_back_to_default() -> None:
    """A missing window value falls back to 100_000."""
    rail = ContextHeadroomRail()
    ratio = rail._usage_ratio(_ctx(total_tokens=50_000, window_tokens=None))
    assert ratio is not None
    assert abs(ratio - 0.5) < 1e-9


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
        "_context_headroom_rail",
    ):
        setattr(adapter, attr, None)
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._skill_include_tools_for_profile = MagicMock(return_value=False)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=False)
    return adapter


def test_hot_reload_includes_context_headroom_rail() -> None:
    """The rail is rebuilt from current config and included on hot reload."""
    adapter = _make_adapter({})
    config_base = {
        "context_headroom": {
            "enabled": True,
            "warn_ratio": 0.70,
            "critical_ratio": 0.90,
        }
    }

    rails = adapter._get_current_agent_rails({"react": {}}, config_base)

    ch = [r for r in rails if isinstance(r, ContextHeadroomRail)]
    assert len(ch) == 1
    assert ch[0]._warn_ratio == 0.70
    assert adapter._context_headroom_rail is ch[0]


def test_hot_reload_disable_tears_down_rail() -> None:
    """Disabling the rail keeps the old instance in the list so it is torn down."""
    adapter = _make_adapter({})
    config_base_on = {"context_headroom": {"enabled": True}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_on)
    old_rail = next(r for r in rails if isinstance(r, ContextHeadroomRail))

    config_base_off = {"context_headroom": {"enabled": False}}
    rails = adapter._get_current_agent_rails({"react": {}}, config_base_off)

    assert old_rail in rails
    assert adapter._context_headroom_rail is None


def test_build_rail_tolerates_null_ratios() -> None:
    """Null ratio config falls back to defaults instead of crashing float()."""
    rail = JiuWenSwarmDeepAdapter._build_context_headroom_rail(
        {"context_headroom": {"enabled": True, "warn_ratio": None}}
    )

    assert rail is not None
    assert rail._warn_ratio == 0.60
    assert rail._critical_ratio == 0.80
