# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration and mode-isolation tests for ``long_horizon_task``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from openjiuwen.agent_teams.harness.manifest import get_catalog

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.common import tool_display
from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


def _tool_card_name(tool: Any) -> str:
    card = getattr(tool, "card", None)
    return str(getattr(card, "name", "") or "")


def build_spec_for_mode(mode: str) -> SimpleNamespace:
    """Assemble mode-specific capability surface for isolation assertions."""
    registry.register_swarm_providers()
    if mode == "agent":
        from jiuwenswarm.agents.harness.common.long_horizon.tools import (
            get_decorated_tools,
        )
        from jiuwenswarm.agents.swarm.providers import long_horizon as lh_provider

        tools = list(get_decorated_tools())
        provider_tools = list(lh_provider.build_long_horizon_tools({}, SimpleNamespace()))
        return SimpleNamespace(
            mode="agent",
            tools=tools,
            provider_tools=provider_tools,
            tool_specs=[],
        )

    swarm_mode = "team" if mode == "team" else "code.team"
    _rails, tool_specs = build_member_capability_specs({}, swarm_mode, "leader")
    return SimpleNamespace(mode=mode, tools=[], provider_tools=[], tool_specs=tool_specs)


def builtin_tool_names(spec: SimpleNamespace) -> set[str]:
    """Concrete tool card names (agent) or expanded member provider surface."""
    if spec.mode == "agent":
        names = {_tool_card_name(tool) for tool in spec.tools}
        names.update(_tool_card_name(tool) for tool in spec.provider_tools)
        return {name for name in names if name}

    provider_name = getattr(registry, "LONG_HORIZON", "swarm.long_horizon")
    names: set[str] = set()
    for tool_spec in spec.tool_specs:
        provider_type = str(getattr(tool_spec, "type", "") or "")
        names.add(provider_type)
        if provider_type == provider_name:
            names.add("long_horizon_task")
    return names


def test_agent_mode_contains_long_horizon_task():
    spec = build_spec_for_mode("agent")
    assert "long_horizon_task" in builtin_tool_names(spec)


def test_long_horizon_tool_description_is_bilingual():
    from jiuwenswarm.agents.harness.common.long_horizon.tools import (
        _DESCRIPTION_CN,
        _DESCRIPTION_EN,
        get_decorated_tools,
    )

    cn_card = get_decorated_tools(language="cn")[0].card
    en_card = get_decorated_tools(language="en")[0].card
    assert cn_card.description == _DESCRIPTION_CN
    assert en_card.description == _DESCRIPTION_EN
    assert "长程" in cn_card.description
    assert "long-horizon" in en_card.description.lower()
    assert cn_card.description != en_card.description
    assert "ask_user" in cn_card.description
    assert "ask_user" in en_card.description
    assert "T-30" not in cn_card.description
    assert "T-30" not in en_card.description
    assert "due_at" in cn_card.description
    assert "due_at" in en_card.description
    assert "YYYY-MM-DD" in cn_card.description
    assert "09:00" in cn_card.description
    assert "conclusion" not in cn_card.description


def test_long_horizon_tool_schema_exposes_stage_time():
    from jiuwenswarm.agents.harness.common.long_horizon.tools import get_decorated_tools

    schema = get_decorated_tools(language="cn")[0].card.input_params
    props = schema["properties"]
    assert set(props) == {
        "action",
        "title",
        "stages",
        "task_id",
        "brief",
        "stage_id",
        "stage_action",
    }
    stage_props = props["stages"]["items"]["properties"]
    assert set(stage_props) == {"id", "title", "due_at", "plan"}
    assert props["stages"]["items"]["required"] == ["title", "due_at", "plan"]
    assert "YYYY-MM-DD HH:MM" in stage_props["due_at"]["description"]
    assert "update" in props["action"]["description"]


@pytest.mark.parametrize("mode", ["team", "code"])
def test_non_agent_modes_do_not_contain_long_horizon_task(mode):
    spec = build_spec_for_mode(mode)
    names = builtin_tool_names(spec)
    assert "long_horizon_task" not in names
    assert getattr(registry, "LONG_HORIZON", "swarm.long_horizon") not in names


def test_long_horizon_provider_is_registered_in_catalog():
    registry.register_swarm_providers()
    catalog = get_catalog()
    assert getattr(registry, "LONG_HORIZON", "swarm.long_horizon") in catalog


def test_agent_eager_tools_exclude_long_horizon_task():
    config = get_config()
    react = config.get("react") if isinstance(config, dict) else {}
    lazy = react.get("tool_lazy_load") if isinstance(react, dict) else {}
    eager = lazy.get("eager_tools") if isinstance(lazy, dict) else []
    assert "long_horizon_task" not in list(eager or [])
    assert "long_horizon_task" not in interface_deep._DEFAULT_PROGRESSIVE_EAGER_TOOLS


def test_tool_display_names_long_horizon_task():
    assert tool_display._VERB_BY_TOOL.get("long_horizon_task")
    assert tool_display.build_tool_display_name(
        "long_horizon_task", {"action": "draft", "title": "年度发布"}
    )


def test_config_yaml_eager_tools_exclude_long_horizon_task():
    from pathlib import Path

    config_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "resources"
        / "config.yaml"
    )
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    eager = (
        payload.get("react", {})
        .get("tool_lazy_load", {})
        .get("eager_tools", [])
    )
    assert "long_horizon_task" not in eager
