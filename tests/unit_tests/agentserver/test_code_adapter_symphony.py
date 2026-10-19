# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Symphony capabilities in beta3's Code single-agent assembly path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_code


@pytest.mark.asyncio
async def test_code_create_syncs_symphony_before_rail_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    config_base = {
        "react": {"agent_name": "code-agent"},
        "modes": {"code": {"rails": [], "tools": []}},
        "symphony": {"enabled": True},
    }
    events: list[str] = []
    symphony_names = [
        "symphony_read_graph", "symphony_refresh_graph", "symphony_compose_graph",
    ]
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    instance = SimpleNamespace(
        ensure_initialized=AsyncMock(side_effect=lambda: events.append("rail_startup")),
        _registered_rails=[],
    )

    def sync_symphony(_config):
        events.append("sync_symphony")
        adapter._symphony_tools = [
            SimpleNamespace(
                card=SimpleNamespace(name=name, id=name, stateless=True),
            )
            for name in symphony_names
        ]

    def create_agent(**kwargs):
        assert events == ["sync_symphony"]
        assert [card.name for card in kwargs["tools"]] == symphony_names
        events.append("create_agent")
        return instance

    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(interface_code, "get_agent_workspace_dir", lambda: tmp_path / "workspace")
    monkeypatch.setattr(interface_code, "Workspace", lambda **_kwargs: SimpleNamespace())
    monkeypatch.setattr(interface_code, "_set_workspace_coding_memory_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(interface_code, "configure_workspace_memory", lambda *_args: None)
    monkeypatch.setattr(interface_code, "create_deep_agent", create_agent)
    monkeypatch.setattr(interface_code, "run_assembly_hooks", AsyncMock())
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: False)
    monkeypatch.setattr(adapter, "_create_model", lambda _config: object())
    monkeypatch.setattr(adapter, "_get_tool_cards", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "_sync_symphony_tools_for_runtime", sync_symphony)
    monkeypatch.setattr(adapter, "_build_agent_rails", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(adapter, "_create_sys_operation", lambda: object())
    monkeypatch.setattr(adapter, "_build_configured_subagents", lambda *_args: ([], False))
    monkeypatch.setattr(adapter, "_install_structured_static_prompt_sections", MagicMock())
    monkeypatch.setattr(adapter, "_ensure_project_gitignore_agent_history", MagicMock())
    monkeypatch.setattr(adapter, "_seed_runtime_cwd", MagicMock())
    monkeypatch.setattr(adapter, "_ensure_cron_tools_registered", MagicMock())
    monkeypatch.setattr(adapter, "_register_mcp_servers_from_config", AsyncMock())

    await adapter.create_instance({"channel_id": "web", "project_dir": str(tmp_path / "project")})

    assert events == ["sync_symphony", "create_agent", "rail_startup"]
    assert [card.name for card in adapter._tool_cards] == symphony_names


def test_code_mounts_one_symphony_rail_and_allows_it_in_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(adapter, "_instantiate_rails", lambda rail_infos, _config: rail_infos)

    rail_infos = adapter._build_agent_rails(
        {},
        {"models": {}, "modes": {"code": {"rails": ["SymphonyOrchestrationRail"]}}},
    )

    assert "SymphonyOrchestrationRail" in adapter._FIXED_RAIL_NAMES
    assert [info.attr_name for info in rail_infos].count("_symphony_orchestration_rail") == 1
    assert {
        "tool_search", "tool_call", "symphony_read_graph",
        "symphony_refresh_graph", "symphony_compose_graph",
    } <= set(interface_code._CODE_PLAN_ALLOWED_TOOLS)
