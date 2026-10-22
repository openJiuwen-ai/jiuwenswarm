# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code sessions must filter large tool catalogs from their first model call."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import InvokeToolInput
from jiuwenswarm.agents.harness.common.tools.tools_search_tool import ToolsSearchInput
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter


CODE_TOOLS = [
    "lsp",
    "coding_memory_read",
    "coding_memory_write",
    "coding_memory_edit",
    "switch_mode",
    "enter_plan_mode",
    "exit_plan_mode",
]


def _tool_policy_rails(monkeypatch, config, sub_mode):
    adapter = JiuwenSwarmCodeAdapter()
    instantiate = adapter._instantiate_rails

    def build_tool_policy(infos, base):
        # Keep the production rail plan and instantiation; unrelated rails may
        # start LSP/model services and are outside this regression's boundary.
        return instantiate(
            [info for info in infos if info.attr_name == "_progressive_tool_rail"],
            base,
        )

    monkeypatch.setattr(adapter, "_instantiate_rails", build_tool_policy)
    monkeypatch.setattr(adapter, "_load_extra_rails_from_env", lambda: [])
    monkeypatch.setattr(adapter, "_bind_checkpointer_to_rails", lambda: None)
    rails = adapter._build_agent_rails(config, {}, sub_mode=sub_mode)
    return adapter, rails


def _initial_rail(monkeypatch, config, sub_mode="normal"):
    adapter, rails = _tool_policy_rails(monkeypatch, config, sub_mode)
    rail = adapter._progressive_tool_rail
    assert rail is not None
    assert rails.count(rail) == 1
    return adapter, rail


def _card(name, description="tool description"):
    return ToolCard(id=f"id-{name}", name=name, description=description, input_params={})


def _context(rail, cards, model_name="glm-5"):
    agent = SimpleNamespace(
        model_name=model_name,
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=None,
    )
    rail.init(agent)
    return SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", ["team", "TEAM", " team "])
async def test_team_profile_keeps_collaboration_and_workspace_tools_direct(monkeypatch, sub_mode):
    config = {
        "tool_lazy_load": {
            "enabled": True, "enable_for_models": ["glm"], "eager_tools": ["read_file"]
        }
    }
    adapter, rails = _tool_policy_rails(monkeypatch, config, sub_mode)
    cards = [_card(name) for name in (
        "send_message", "create_task", "read_file", "web_fetch_webpage",
        "user_todos", "skill_retrieval",
    )]
    agent = SimpleNamespace(
        model_name="glm-5",
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=None,
    )
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))
    for rail in rails:
        if isinstance(rail, ProgressiveToolRail):
            rail.init(agent)
            await rail.before_model_call(ctx)

    assert ctx.inputs.tools == cards
    assert adapter._progressive_tool_rail is None


@pytest.mark.parametrize("enabled", [None, False, True])
@pytest.mark.parametrize("has_stale_rail", [False, True])
def test_team_reload_plan_does_not_mount_progressive_rail(monkeypatch, enabled, has_stale_rail):
    config = (
        {"tool_lazy_load": {"enabled": enabled, "enable_for_models": ["glm"]}}
        if enabled is not None else {}
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = SimpleNamespace(_jiuwenswarm_code_team_member=True)
    adapter._code_agent_rail = None
    adapter._code_plan_approval_rail = None
    old = ProgressiveToolRail(eager_tools=["read_file"]) if has_stale_rail else None
    adapter._progressive_tool_rail = old
    for builder in (
        "_build_skill_rail", "_build_skill_credential_injection_rail",
        "_build_skill_active_state_rail", "_update_permission_rail",
    ):
        monkeypatch.setattr(adapter, builder, MagicMock(return_value=None))

    rails, retired = adapter._get_current_agent_rails(config, {"react": config})

    assert not any(isinstance(rail, ProgressiveToolRail) for rail in rails)
    assert retired == ([old] if old is not None else [])
    if enabled is None:
        assert "tool_lazy_load" not in config


@pytest.mark.asyncio
@pytest.mark.parametrize("has_stale_rail", [False, True])
@pytest.mark.parametrize("partial_reload", [False, True])
async def test_team_config_reload_clears_stale_rail_and_keeps_tools_direct(
    monkeypatch, has_stale_rail, partial_reload,
):
    config = (
        {} if partial_reload else
        {"tool_lazy_load": {"enabled": True, "enable_for_models": ["glm"]}}
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_agent_rail = None
    adapter._code_plan_approval_rail = None
    instance = SimpleNamespace(
        _jiuwenswarm_code_team_member=True,
        configure=MagicMock(), ensure_initialized=AsyncMock(), unregister_rail=AsyncMock(),
    )
    adapter._instance = instance
    old = ProgressiveToolRail(eager_tools=["read_file"]) if has_stale_rail else None
    adapter._progressive_tool_rail = old

    async def apply_snapshot(*args):
        adapter._config_cache = config.copy()
        adapter._config_base_cache = {"react": config}
        return adapter._config_base_cache

    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", apply_snapshot)
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", lambda *args: (None, None, None))
    monkeypatch.setattr(adapter, "_create_model", lambda *args: object())
    monkeypatch.setattr(adapter, "_make_deep_agent_config", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(adapter, "_omit_unchanged_reload_fields", lambda *args: ({}, {}))
    for name in (
        "_build_skill_rail", "_build_skill_credential_injection_rail",
        "_build_skill_active_state_rail", "_update_permission_rail",
        "_sync_multimodal_tools_for_runtime", "_sync_paid_search_tool_for_runtime",
        "_sync_symphony_tools_for_runtime", "_sync_skill_retrieval_tools_for_runtime",
        "_restore_omitted_reload_fields", "_commit_reload_fingerprints",
        "_sync_active_evolution_review_agent_after_reload", "_rebind_late_read_rails_to_local",
    ):
        monkeypatch.setattr(adapter, name, MagicMock(return_value=None))
    for name in (
        "load_user_rails", "_sync_skill_retrieval_prompt_rail_for_runtime",
        "_reconcile_evolution_rails", "_sync_mcp_servers_for_runtime",
        "_fan_out_reload_to_session_adapters", "_handle_memory_rail_by_config",
        "_handle_external_memory_rail_by_config", "_handle_office_ace_user_profile_rail_by_config",
    ):
        monkeypatch.setattr(adapter, name, AsyncMock())

    # A second reload must not resurrect a rail removed by the first one.
    for _ in range(2):
        result = await adapter.reload_agent_config({"react": config})
        assert result.applied is True
        assert adapter._progressive_tool_rail is None
        rails = instance.configure.call_args.args[0].rails
        assert not any(isinstance(rail, ProgressiveToolRail) for rail in rails)
        cards = [_card(name) for name in ("read_file", "send_message", "web_fetch_webpage")]
        ctx = SimpleNamespace(inputs=SimpleNamespace(tools=list(cards)))
        for rail in rails:
            await rail.before_model_call(ctx)
        assert ctx.inputs.tools == cards
    if old is not None:
        instance.unregister_rail.assert_awaited_once_with(old)
    else:
        instance.unregister_rail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", ["normal", "plan"])
async def test_first_call_filters_large_mcp_catalog_and_preserves_coding_tools(monkeypatch, sub_mode):
    config = {
        "tool_lazy_load": {
            "enabled": True, "enable_for_models": ["glm"], "eager_tools": ["read_file"]
        }
    }
    adapter, rail = _initial_rail(monkeypatch, config, sub_mode)
    eager = [_card(name) for name in ["tools_search", "invoke_tool", "read_file", *CODE_TOOLS]]
    deferred = [_card(f"wps_{i}", "large schema description " * 200) for i in range(666)]
    ctx = _context(rail, eager + deferred)
    adapter._sync_office_claw_allowlist_to_progressive_rail(
        [card.id for card in deferred],
        delivery_thread_id="thread-code",
        invocation_id="turn-code",
    )

    await rail.before_model_call(ctx)

    assert {tool.name for tool in ctx.inputs.tools} == {tool.name for tool in eager}
    assert len(rail._cached_deferred_tool_infos) == 666
    assert rail._office_claw_active_tool_ids == frozenset(card.id for card in deferred)
    assert sum(len(t.description) for t in ctx.inputs.tools) < 1000
    target = SimpleNamespace(card=deferred[-1], invoke=AsyncMock(return_value="wps-result"))
    monkeypatch.setattr(
        rail, "_lookup_tool_instance",
        lambda tool_id: target if tool_id == target.card.id else None,
    )
    result = await rail._search_tools(None, ToolsSearchInput(tool_name="wps_665"))
    assert result["success"] is True
    result = await rail._invoke_target_tool(
        None, InvokeToolInput(tool_name="wps_665", arguments={})
    )
    assert result["success"] is True
    target.invoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,model_name", [(False, "glm-5"), (True, "other-model")])
async def test_config_switch_and_model_whitelist_remain_effective(monkeypatch, enabled, model_name):
    _, rail = _initial_rail(monkeypatch, {
        "tool_lazy_load": {"enabled": enabled, "enable_for_models": ["glm"], "eager_tools": []}
    })
    cards = [_card("read_file"), _card("wps_deferred")]
    ctx = _context(rail, cards, model_name)
    await rail.before_model_call(ctx)
    assert ctx.inputs.tools == cards


def test_reload_replaces_rail_once_and_keeps_code_eager_policy(monkeypatch):
    config = {"tool_lazy_load": {"enabled": True, "eager_tools": ["read_file"]}}
    adapter, old = _initial_rail(monkeypatch, config)
    adapter._code_agent_rail = None
    adapter._code_plan_approval_rail = None
    adapter._active_office_claw_mcp = SimpleNamespace(
        tool_ids=("id-wps_1",), invocation_id="turn-code",
        tool_instances=[SimpleNamespace(_params={"env": {"OFFICE_CLAW_THREAD_ID": "thread-code"}})],
    )
    for builder in (
        "_build_skill_rail", "_build_skill_credential_injection_rail",
        "_build_skill_active_state_rail",
    ):
        monkeypatch.setattr(adapter, builder, lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_update_permission_rail", lambda *args: None)
    rails, retired = adapter._get_current_agent_rails(config, {"react": config})
    replacement = adapter._progressive_tool_rail
    assert replacement is not old
    assert rails.count(replacement) == 1
    assert old not in rails
    assert retired == []  # configure replaces the old rail of the same type.
    assert set(CODE_TOOLS) <= set(replacement.eager_tools)
    assert replacement._office_claw_active_tool_ids == frozenset({"id-wps_1"})
    assert replacement._office_claw_delivery_thread_id == "thread-code"
    assert replacement._office_claw_invocation_id == "turn-code"
    assert config["tool_lazy_load"]["eager_tools"] == ["read_file"]


@pytest.mark.asyncio
async def test_code_eager_additions_do_not_bypass_disabled_tools(monkeypatch):
    _, rail = _initial_rail(monkeypatch, {
        "tool_lazy_load": {"enabled": True, "eager_tools": []},
        "disabled_tools": ["coding_memory_write", "wps_disabled"],
    })
    cards = [_card("coding_memory_read"), _card("coding_memory_write"), _card("wps_disabled")]
    ctx = _context(rail, cards)
    await rail.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == ["coding_memory_read"]
    assert rail._cached_deferred_tool_infos == []
    result = await rail._invoke_target_tool(
        None, InvokeToolInput(tool_name="wps_disabled", arguments={})
    )
    assert result["success"] is False
