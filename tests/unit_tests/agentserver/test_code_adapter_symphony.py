# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for Symphony capabilities in the Code single-agent path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.code.spec import CodeBuildContext
from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


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
