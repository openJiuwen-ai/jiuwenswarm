# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.agents.harness.common.tools import user_todo_tool
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.agents.harness.code.spec import CodeBuildContext
from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


# --- from test_code_adapter_runtime_config.py ---

@pytest.mark.asyncio
async def test_runtime_config_updates_browser_prompt_and_permission_rails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Per-request setup supports current browser and permission rail APIs."""
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = SimpleNamespace(
        ability_manager=SimpleNamespace(add=MagicMock()),
    )
    adapter._project_dir = str(tmp_path)
    adapter._workspace_dir = str(tmp_path)
    adapter._agent_workspace_dir = str(tmp_path)
    adapter._runtime_prompt_rail = None
    adapter._subagent_rail = BrowserTaskPromptRail()
    adapter._project_memory_rail = None
    adapter._permission_rail = MagicMock()

    update_rails = AsyncMock()
    monkeypatch.setattr(adapter, "_seed_runtime_cwd", MagicMock())
    monkeypatch.setattr(adapter, "_resolve_output_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_write_runtime_state", MagicMock())
    monkeypatch.setattr(adapter, "_update_rails_for_mode", update_rails)
    monkeypatch.setattr(adapter, "_set_user_interaction_enabled", AsyncMock())
    monkeypatch.setattr(adapter, "_update_tools_for_mode", AsyncMock())
    monkeypatch.setattr(adapter, "_update_session_tools", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_acp_runtime_tools", MagicMock())
    monkeypatch.setattr(adapter, "_update_prompt_for_mode", MagicMock())
    monkeypatch.setattr(adapter, "_register_shared_tool", MagicMock())
    monkeypatch.setattr(user_todo_tool, "set_global_workspace_dir", MagicMock())
    monkeypatch.setattr(user_todo_tool, "set_global_channel_id", MagicMock())
    monkeypatch.setattr(user_todo_tool, "get_decorated_tools", lambda: [])

    await adapter._update_runtime_config(
        adapter._RuntimeConfig(
            session_id="tui_session_1",
            request_id="request_1",
            mode="code",
            channel_id="tui",
            project_dir=str(tmp_path),
        )
    )

    update_rails.assert_awaited_once_with("code")
    adapter._permission_rail.set_trusted_dirs.assert_called_once_with(
        [str(tmp_path.resolve())]
    )

# --- from test_code_adapter_symphony.py ---

@pytest.mark.asyncio
async def test_default_code_create_syncs_symphony_before_rail_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A fresh Code session must expose Symphony on its very first turn."""
    config_base = {
        "react": {"agent_name": "code-agent"},
        "modes": {"code": {"rails": [], "tools": []}},
        "symphony": {"enabled": True},
    }
    events: list[str] = []
    symphony_names = [
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    ]

    class FakeResourceManager:
        def __init__(self) -> None:
            self.added: list[str] = []

        def add_tool(self, tool, **_kwargs) -> None:
            self.added.append(tool.card.name)

    resource_manager = FakeResourceManager()

    class FakeAbilityManager:
        def __init__(self) -> None:
            self.owner_id: str | None = None
            self.cards: dict[str, object] = {}

        def set_owner_id(self, owner_id: str) -> None:
            self.owner_id = owner_id

        def add(self, card) -> None:
            self.cards[card.name] = card

    ability_manager = FakeAbilityManager()

    async def ensure_initialized() -> None:
        assert ability_manager.owner_id == "code-symphony-owner"
        assert list(ability_manager.cards) == symphony_names
        assert [card.name for card in adapter._tool_cards] == symphony_names
        events.append("rail_startup")

    instance = SimpleNamespace(
        deep_config=SimpleNamespace(model=None, tools=[], tool_owner_id=None),
        ability_manager=ability_manager,
        configured_rails=lambda: [],
        ensure_initialized=ensure_initialized,
        _registered_rails=[],
    )
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    context = CodeBuildContext(
        adapter=adapter,
        config_base=config_base,
        react_config=config_base["react"],
        tool_owner_id="code-symphony-owner",
    )
    built_spec = SimpleNamespace(build=lambda _context: instance)

    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_code,
        "get_agent_workspace_dir",
        lambda: tmp_path / "agent-workspace",
    )
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: False)
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    monkeypatch.setattr(adapter, "_create_model", lambda _config: object())
    monkeypatch.setattr(
        adapter,
        "_create_sys_operation",
        lambda: SimpleNamespace(id="code-symphony-sysop"),
    )
    monkeypatch.setattr(
        adapter,
        "_build_code_spec_snapshot",
        MagicMock(return_value=(built_spec, context)),
    )

    monkeypatch.setattr(adapter, "_seed_runtime_cwd", MagicMock())
    monkeypatch.setattr(adapter, "_ensure_cron_tools_registered", MagicMock())
    monkeypatch.setattr(adapter, "_register_mcp_servers_from_config", AsyncMock())
    monkeypatch.setattr(adapter, "_load_active_packages", AsyncMock())
    monkeypatch.setattr(adapter, "load_user_rails", AsyncMock())

    await adapter.create_instance(
        {"channel_id": "web", "project_dir": str(tmp_path / "project")}
    )

    assert events == ["rail_startup"]
    assert resource_manager.added == symphony_names
    assert [card.id for card in adapter._tool_cards] == symphony_names
    assert all(card.stateless for card in adapter._tool_cards)
    assert adapter._config_base_cache == config_base
    assert adapter._config_base_cache is not config_base

def test_code_mounts_one_symphony_rail_and_allows_it_in_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(
        adapter,
        "_instantiate_rails",
        lambda rail_infos, _config: rail_infos,
    )

    rail_infos = adapter._build_agent_rails(
        {},
        {
            "models": {},
            "modes": {
                "code": {"rails": ["SymphonyOrchestrationRail"]},
            },
        },
    )

    assert "SymphonyOrchestrationRail" in adapter._FIXED_RAIL_NAMES
    assert [info.attr_name for info in rail_infos].count(
        "_symphony_orchestration_rail"
    ) == 1
    assert {
        "tool_search",
        "tool_call",
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    } <= set(interface_code._CODE_PLAN_ALLOWED_TOOLS)

