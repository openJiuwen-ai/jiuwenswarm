# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeAdapter coding-memory configuration."""

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface_code


def test_coding_memory_rail_uses_spec_snapshot_and_rebuilds_on_embed_change(
    monkeypatch,
    tmp_path,
):
    persisted_config = {
        "modes": {"code": {"memory": {"enabled": False}}},
        "embed": {"embed_model": "persisted"},
    }
    monkeypatch.setattr(interface_code, "get_config", lambda: persisted_config)

    observed_configs = []

    def fake_create_coding_memory_rail(*, project_dir, agent_workspace_dir, config):
        del project_dir, agent_workspace_dir
        observed_configs.append(config)
        return object()

    monkeypatch.setattr(
        interface_code,
        "create_coding_memory_rail",
        fake_create_coding_memory_rail,
    )

    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path)
    adapter._workspace_dir = str(tmp_path)
    adapter._agent_workspace_dir = str(tmp_path / "agent")
    first_snapshot = {
        "modes": {"code": {"memory": {"enabled": True}}},
        "embed": {"embed_model": "first"},
    }
    second_snapshot = {
        "modes": {"code": {"memory": {"enabled": True}}},
        "embed": {"embed_model": "second"},
    }

    with adapter._code_spec_config_scope(first_snapshot):
        first = adapter._build_coding_memory_rail()
    with adapter._code_spec_config_scope(first_snapshot):
        reused = adapter._build_coding_memory_rail()
    with adapter._code_spec_config_scope(second_snapshot):
        rebuilt = adapter._build_coding_memory_rail()

    assert first is reused
    assert rebuilt is not first
    assert observed_configs == [first_snapshot, second_snapshot]


def test_configure_code_team_member_uses_fallback_for_rail_without_workspace_node(
    monkeypatch,
    tmp_path,
):
    """Coding Memory keeps the fallback project identity without a workspace node."""
    global_workspace = tmp_path / "global_agent_workspace"
    fallback_workspace = tmp_path / "fallback_project_workspace"
    observed_project_dirs = {}

    monkeypatch.setattr(
        interface_code,
        "get_config",
        lambda: {"react": {"workspace_dir": str(fallback_workspace)}},
    )
    monkeypatch.setattr(
        interface_code,
        "get_agent_workspace_dir",
        lambda: global_workspace,
    )
    monkeypatch.setattr(interface_code, "is_memory_enabled", lambda mode, config: True)
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
        "_build_configured_subagents",
        lambda self, model, react_config, config_base: ([], False),
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "merge_member_mcp_configs",
        lambda self, agent, config_base: 0,
    )

    def fake_create_coding_memory_rail(*, project_dir, agent_workspace_dir, config):
        observed_project_dirs["rail"] = project_dir
        observed_project_dirs["agent_workspace"] = agent_workspace_dir
        return object()

    def build_only_coding_memory_rail(self, react_config, config_base, *, mode):
        rail = self._build_coding_memory_rail()
        return [rail] if rail is not None else []

    monkeypatch.setattr(
        interface_code,
        "create_coding_memory_rail",
        fake_create_coding_memory_rail,
    )
    monkeypatch.setattr(
        interface_code.JiuwenSwarmCodeAdapter,
        "_build_agent_rails",
        build_only_coding_memory_rail,
    )

    class Workspace:
        root_path = None

        def set_directory(self, directory):
            observed_project_dirs["workspace_path"] = directory["path"]

    class AbilityManager:
        @staticmethod
        def list():
            return []

        @staticmethod
        def add(card):
            raise AssertionError("no tool cards should be added in this test")

    agent = SimpleNamespace(
        card=SimpleNamespace(id="counter-1", name="Counter 1"),
        deep_config=SimpleNamespace(
            workspace=Workspace(),
            model=None,
            sys_operation=None,
            subagents=[],
            mcps=[],
        ),
        ability_manager=AbilityManager(),
        add_rail=lambda rail: None,
    )

    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter.configure_team_member_agent(agent)

    expected_project_dir = str(fallback_workspace)
    assert observed_project_dirs["rail"] == expected_project_dir
    assert observed_project_dirs["agent_workspace"] == str(global_workspace)
    assert "workspace_path" not in observed_project_dirs
