"""动态专家的配置门禁与代码默认值。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from typing import Any

import pytest
import yaml

from jiuwenswarm.common.config import is_dynamic_experts_enabled
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.symphony.expert_team import PLAN_TOOL_NAME


def test_packaged_dynamic_default_is_independent_from_skill_symphony():
    config_path = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["symphony"]["enabled"] is False
    assert config["symphony"]["dynamic_experts"]["enabled"] is None
    assert is_dynamic_experts_enabled(config) is True


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, True),
        ({}, True),
        ({"symphony": {"dynamic_experts": {"enabled": None}}}, True),
        ({"symphony": {"dynamic_experts": {"enabled": False}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": "true"}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": 1}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": True}}}, True),
    ],
)
def test_dynamic_experts_uses_code_default_unless_explicitly_set(config: Any, expected: bool) -> None:
    assert is_dynamic_experts_enabled(config) is expected


@pytest.mark.parametrize(("adapter_type", "mode", "sub_mode", "eligible"), [
    (JiuWenSwarmDeepAdapter, "agent", None, True),
    (JiuWenSwarmDeepAdapter, "agent", "plan", True),
    (JiuWenSwarmDeepAdapter, "agent.plan", None, True),
    (JiuwenSwarmCodeAdapter, "code", "normal", True),
    (JiuwenSwarmCodeAdapter, "code.normal", None, True),
    (JiuwenSwarmCodeAdapter, "code.plan", None, True),
    (JiuwenSwarmCodeAdapter, "design", None, True),
    (JiuwenSwarmCodeAdapter, "design.plan", None, True),
    (JiuWenSwarmDeepAdapter, "team", None, False),
    (JiuWenSwarmDeepAdapter, "agent.fast", None, False),
    (JiuwenSwarmCodeAdapter, "code", "team", False),
    (JiuwenSwarmCodeAdapter, "code.team", None, False),
    (JiuwenSwarmCodeAdapter, "design.team", None, False),
    (JiuwenSwarmCodeAdapter, "team.plan", None, False),
])
@pytest.mark.parametrize("enabled", [False, True])
def test_dynamic_expert_rails_and_tools_share_single_profile_gate(
    monkeypatch, adapter_type, mode, sub_mode, eligible, enabled,
):
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    adapter = adapter_type()
    adapter._session_instance_mode = mode
    adapter._session_instance_sub_mode = sub_mode
    adapter._initialize_dynamic_experts({"symphony": {"dynamic_experts": {"enabled": enabled}}})
    monkeypatch.setattr(adapter, "_instantiate_rails", lambda infos, _config: [info.attr_name for info in infos])
    registered = []
    monkeypatch.setattr(adapter, "_register_agent_owned_tool", lambda tool, owner: registered.append((tool, owner)))

    names = adapter._build_agent_rails({}, {"permissions": {"enabled": False}}, mode=mode)
    cards = adapter._register_dynamic_expert_tools("session-owner")

    assert ("_dynamic_expert_rail" in names) is (enabled and eligible)
    assert ("_dynamic_expert_confirmation_rail" in names) is (enabled and eligible)
    assert [card.name for card in cards] == ([PLAN_TOOL_NAME] if enabled and eligible else [])
    assert [owner for _, owner in registered] == (["session-owner"] if enabled and eligible else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_type", "mode", "sub_mode"), [
    (JiuWenSwarmDeepAdapter, "agent", None),
    (JiuwenSwarmCodeAdapter, "code", "normal"),
    (JiuwenSwarmCodeAdapter, "design", None),
])
async def test_cold_session_and_rebuild_initialize_switch_before_tools(monkeypatch, adapter_type, mode, sub_mode):
    config = {"symphony": {"dynamic_experts": {"enabled": True}}}
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_code, "get_config", lambda: config)
    monkeypatch.setattr(interface_deep, "load_dotenv_runtime", lambda **_: None)
    monkeypatch.setattr(adapter_type, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter_type, "_refresh_multimodal_configs", lambda *_: None)
    root = adapter_type()
    await root.create_instance(mode=mode, sub_mode=sub_mode)
    assert root._instance is None and root._dynamic_experts_enabled

    child = root._new_session_scoped_adapter("dynamic-session")
    assert type(child) is adapter_type and not child._dynamic_experts_enabled
    monkeypatch.setattr(child, "_create_model", lambda *_: object())
    monkeypatch.setattr(child, "_try_init_a2x_client", AsyncMock())
    observed = []

    class ToolsReached(Exception):
        pass

    async def collect_tools(_owner):
        observed.append((child._dynamic_experts_enabled, child._session_instance_mode, child._session_instance_sub_mode))
        raise ToolsReached

    monkeypatch.setattr(child, "_get_tool_cards", collect_tools)
    for enabled in (True, False, True):
        config["symphony"]["dynamic_experts"]["enabled"] = enabled
        with pytest.raises(ToolsReached):
            await child.create_instance(mode=mode, sub_mode=sub_mode)
    assert observed == [(value, mode, sub_mode) for value in (True, False, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["code", "design"])
async def test_code_and_design_tool_menu_registers_session_owned_planner(monkeypatch, mode):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._session_instance_mode = mode
    adapter._initialize_dynamic_experts({"symphony": {"dynamic_experts": {"enabled": True}}})
    monkeypatch.setattr(adapter, "build_code_tool_cards", lambda _: [])
    registered = []
    monkeypatch.setattr(adapter, "_register_agent_owned_tool", lambda tool, owner: registered.append((tool, owner)))
    cards = await adapter._get_tool_cards("code-session")
    assert [card.name for card in cards] == [PLAN_TOOL_NAME]
    assert registered[0][1] == "code-session"
    assert (await registered[0][0].invoke({}))["reason"] == "expert_plan_unavailable"
