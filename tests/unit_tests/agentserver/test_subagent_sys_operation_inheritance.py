# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subagents must share the parent agent's SysOperation.

The SysOperation is the filesystem boundary the user configured (local full
access / jiuwenbox sandbox / allow-deny paths). When a subagent spec leaves it
unset, ``DeepAgent.create_subagent`` mints a fresh ``OperationMode.LOCAL``
SysOperation with ``restrict_to_sandbox`` derived from
``spec.restrict_to_work_dir or deep_config.restrict_to_work_dir`` — which both
locks the subagent inside the workspace in local mode and lets it escape onto
the host in sandbox mode.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.server.runtime.agent_adapter.code_agent_rail import AgentTool
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _agent_def_to_subagent_config,
)


def _spec_by_name(specs: list, name: str) -> SubAgentConfig | None:
    """Return the spec whose agent card carries ``name``."""
    for spec in specs:
        if isinstance(spec, SubAgentConfig) and spec.agent_card.name == name:
            return spec
    return None


def _make_agent_definition(name: str):
    """Build a minimal custom agent definition."""
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

    return AgentDefinition(
        name=name,
        description="test",
        prompt="You are a test agent.",
        source="project",
        tools=["*"],
        when_to_use="testing",
    )


def test_code_subagents_inherit_parent_sys_operation(tmp_path):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    sys_operation = MagicMock()
    adapter._sys_operation = sys_operation

    config = {"subagents": {"code_agent": {"enabled": True}}, "max_iterations": 15}
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False):
        subagents, _ = adapter._build_configured_subagents(MagicMock(), config, {})

    assert subagents is not None
    statusline_spec = _spec_by_name(subagents, "statusline-setup")
    plan_spec = _spec_by_name(subagents, "plan_agent")
    code_spec = _spec_by_name(subagents, "code_agent")
    assert statusline_spec is not None
    assert plan_spec is not None
    assert code_spec is not None
    # create_subagent only honours spec.sys_operation when spec.workspace is set too.
    assert statusline_spec.sys_operation is sys_operation
    assert statusline_spec.workspace == str(tmp_path)
    assert plan_spec.sys_operation is sys_operation
    assert plan_spec.workspace == str(tmp_path)
    assert code_spec.sys_operation is sys_operation
    assert code_spec.workspace == str(tmp_path)


def test_deep_research_subagent_inherits_parent_sys_operation(tmp_path):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = str(tmp_path)
    sys_operation = MagicMock()
    adapter._sys_operation = sys_operation

    config = {"subagents": {"research_agent": {"enabled": True}}, "max_iterations": 15}
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False):
        subagents, _ = adapter._build_configured_subagents(MagicMock(), config, {})

    assert subagents is not None
    research_spec = _spec_by_name(subagents, "research_agent")
    assert research_spec is not None
    assert research_spec.sys_operation is sys_operation
    assert research_spec.workspace == str(tmp_path)


def test_custom_agent_spec_carries_sys_operation_and_workspace(tmp_path):
    sys_operation = MagicMock()

    spec = _agent_def_to_subagent_config(
        _make_agent_definition("reviewer"),
        MagicMock(),
        str(tmp_path),
        None,
        sys_operation,
    )

    assert spec.sys_operation is sys_operation
    assert spec.workspace == str(tmp_path)


def test_agent_tool_subagent_reuses_parent_sys_operation(tmp_path):
    sys_operation = MagicMock()
    parent_config = MagicMock()
    parent_config.model = MagicMock()
    parent_config.workspace = Workspace(root_path=str(tmp_path), language="en")
    parent_config.language = "en"
    parent_config.backend = None
    parent_config.max_iterations = 15
    parent_config.prompt_mode = None
    parent_config.sys_operation = sys_operation

    parent_agent = MagicMock()
    parent_agent.deep_config = parent_config
    parent_agent.ability_manager.list.return_value = []

    agent_def = _make_agent_definition("reviewer")
    tool = AgentTool(MagicMock(), parent_agent, [agent_def])

    with patch(
        "openjiuwen.harness.factory.create_deep_agent",
        return_value=MagicMock(),
    ) as create_deep_agent:
        tool._create_sub_agent(agent_def, "session_custom_reviewer_1")

    assert create_deep_agent.call_args.kwargs["sys_operation"] is sys_operation
