# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration tests for the built-in statusline-setup subagent."""

from __future__ import annotations

from unittest.mock import MagicMock

from openjiuwen.harness.rails import SysOperationRail

from jiuwenswarm.agents.swarm.config_specs import build_member_subagent_specs
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    STATUSLINE_SETUP_AGENT_TYPE,
)
from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService


def test_statusline_setup_is_a_builtin_agent_definition(tmp_path):
    definition = AgentConfigService(tmp_path).get_agent(STATUSLINE_SETUP_AGENT_TYPE)

    assert definition is not None
    assert definition.source == "builtin"
    assert definition.tools == ["Read", "Write", "Edit", "Bash"]
    assert "statusline-setup subagent" in definition.prompt


def test_single_agent_runtime_registers_statusline_setup(monkeypatch, tmp_path):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._sys_operation = None
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: False)

    subagents, _ = adapter._build_configured_subagents(
        MagicMock(),
        {
            "max_iterations": 20,
            "subagents": {
                STATUSLINE_SETUP_AGENT_TYPE: {
                    "enabled": True,
                    "max_iterations": 9,
                }
            },
        },
        {},
    )

    assert subagents is not None
    assert [spec.agent_card.name for spec in subagents] == [STATUSLINE_SETUP_AGENT_TYPE]
    assert subagents[0].max_iterations == 9
    assert subagents[0].enable_task_loop is False
    assert any(isinstance(rail, SysOperationRail) for rail in subagents[0].rails)


def test_single_agent_runtime_does_not_register_when_disabled(monkeypatch, tmp_path):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._sys_operation = None
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: False)

    subagents, _ = adapter._build_configured_subagents(
        MagicMock(),
        {"subagents": {STATUSLINE_SETUP_AGENT_TYPE: {"enabled": False}}},
        {},
    )

    assert subagents is None


def test_code_runtime_registers_statusline_setup_by_default(monkeypatch, tmp_path):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    sys_operation = MagicMock()
    adapter._sys_operation = sys_operation
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: False)

    subagents, _ = adapter._build_configured_subagents(
        MagicMock(),
        {"max_iterations": 20},
        {},
    )

    assert subagents is not None
    assert [spec.agent_card.name for spec in subagents] == [
        STATUSLINE_SETUP_AGENT_TYPE,
        "explore_agent",
        "plan_agent",
    ]
    statusline_spec = subagents[0]
    assert statusline_spec.max_iterations == DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS
    assert statusline_spec.enable_task_loop is False
    assert statusline_spec.sys_operation is sys_operation
    assert statusline_spec.workspace == str(tmp_path)


def test_code_runtime_honors_explicit_statusline_disable(monkeypatch, tmp_path):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    adapter._sys_operation = MagicMock()
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: False)

    subagents, _ = adapter._build_configured_subagents(
        MagicMock(),
        {"subagents": {STATUSLINE_SETUP_AGENT_TYPE: {"enabled": False}}},
        {},
    )

    assert subagents is not None
    assert [spec.agent_card.name for spec in subagents] == [
        "explore_agent",
        "plan_agent",
    ]


def test_code_runtime_honors_explicit_statusline_iteration_budget(
    monkeypatch,
    tmp_path,
):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    adapter._sys_operation = MagicMock()
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: False)

    subagents, _ = adapter._build_configured_subagents(
        MagicMock(),
        {
            "max_iterations": 100,
            "subagents": {
                STATUSLINE_SETUP_AGENT_TYPE: {
                    "enabled": True,
                    "max_iterations": 7,
                }
            },
        },
        {},
    )

    assert subagents is not None
    assert subagents[0].agent_card.name == STATUSLINE_SETUP_AGENT_TYPE
    assert subagents[0].max_iterations == 7


def test_team_member_runtime_registers_same_builtin_subagent():
    specs = build_member_subagent_specs(
        {
            "react": {
                "subagents": {
                    STATUSLINE_SETUP_AGENT_TYPE: {"enabled": True},
                }
            }
        },
        "team",
        "leader",
    )

    assert len(specs) == 1
    assert specs[0].agent_card.name == STATUSLINE_SETUP_AGENT_TYPE
    assert specs[0].factory_name == registry.STATUSLINE_SETUP_AGENT
    assert (
        specs[0].factory_kwargs["max_iterations"]
        == DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS
    )


def test_team_member_runtime_honors_statusline_disable_and_iteration_budget():
    disabled_specs = build_member_subagent_specs(
        {
            "react": {
                "subagents": {
                    STATUSLINE_SETUP_AGENT_TYPE: {"enabled": False},
                }
            }
        },
        "team",
        "leader",
    )
    assert disabled_specs == []

    configured_specs = build_member_subagent_specs(
        {
            "react": {
                "max_iterations": 100,
                "subagents": {
                    STATUSLINE_SETUP_AGENT_TYPE: {
                        "enabled": True,
                        "max_iterations": 7,
                    },
                },
            }
        },
        "team",
        "leader",
    )
    assert len(configured_specs) == 1
    assert configured_specs[0].agent_card.name == STATUSLINE_SETUP_AGENT_TYPE
    assert configured_specs[0].factory_kwargs["max_iterations"] == 7
