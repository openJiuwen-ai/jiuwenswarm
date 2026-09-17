# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team member skill views."""

from types import SimpleNamespace



def test_configure_code_team_member_does_not_mount_coding_memory_workspace_node(monkeypatch, tmp_path):
    """code.team members keep Coding Memory out of the materialized workspace tree."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_code

    global_workspace = tmp_path / "global_agent_workspace"
    member_workspace = tmp_path / "member_workspace"
    parent_project = tmp_path / "project"
    global_workspace.mkdir()
    member_workspace.mkdir()
    parent_project.mkdir()

    monkeypatch.setattr(interface_code, "get_config", lambda: {"react": {}})
    monkeypatch.setattr(interface_code, "get_agent_workspace_dir", lambda: global_workspace)
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_refresh_multimodal_configs",
        lambda self, config: None,
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_create_model",
        lambda self, config: object(),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_create_sys_operation",
        lambda self: object(),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "build_code_tool_cards",
        lambda self, agent_id: [],
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_build_agent_rails",
        lambda self, react_config, config_base, mode: [],
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_build_configured_subagents",
        lambda self, model, react_config, config_base: ([], False),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_extract_enabled_mcp_server_entries",
        lambda self, config_base: [],
    )

    class Workspace:
        def __init__(self, root_path):
            self.root_path = str(root_path)
            self.directories = []

        def set_directory(self, directory):
            self.directories.append(directory)

    class AbilityManager:
        @staticmethod
        def list():
            return []

        @staticmethod
        def add(card):
            raise AssertionError("no tool cards should be added in this test")

    workspace = Workspace(member_workspace)
    agent = SimpleNamespace(
        card=SimpleNamespace(id="counter-1", name="Counter 1"),
        deep_config=SimpleNamespace(
            workspace=workspace,
            model=None,
            sys_operation=None,
            subagents=[],
            mcps=[],
        ),
        ability_manager=AbilityManager(),
        add_rail=lambda rail: None,
    )
    parent_agent = SimpleNamespace(
        _jiuwenswarm_code_project_dir=str(parent_project),
        deep_config=SimpleNamespace(workspace=SimpleNamespace(root_path=str(parent_project))),
    )

    interface_code.configure_code_team_member_agent(
        agent,
        parent_agent=parent_agent,
        member_name="counter-1",
        role="counter",
    )

    assert workspace.directories == []
