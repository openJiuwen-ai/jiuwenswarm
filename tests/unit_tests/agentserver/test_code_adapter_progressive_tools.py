# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code sessions must filter large tool catalogs from their first model call."""

import asyncio
import logging
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

SINGLE_SUB_MODES = [None, "", " \t ", "normal", "plan"]
TEAM_SUB_MODES = ["team", "TEAM", " team "]


def _tool_policy_rails(monkeypatch, config, sub_mode, adapter=None):
    if adapter is None:
        adapter = JiuwenSwarmCodeAdapter()
        adapter._session_instance_sub_mode = sub_mode
    instantiate = adapter._instantiate_rails

    def build_tool_policy(infos, base):
        # Keep the production rail plan and instantiation; unrelated rails may
        # start LSP/model services and are outside this regression's boundary.
        return instantiate(
            [info for info in infos if info.attr_name == "_progressive_tool_rail"],
            base,
        )

    monkeypatch.setattr(adapter, "_instantiate_rails", build_tool_policy)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._load_extra_rails_from_env", lambda: [])
    monkeypatch.setattr(adapter, "_bind_checkpointer_to_rails", lambda: None)
    rails = adapter._build_agent_rails(config, {}, sub_mode=sub_mode)
    return adapter, rails


def _profile_adapter(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: True)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", SINGLE_SUB_MODES + TEAM_SUB_MODES)
async def test_facade_parameterless_rebuild_preserves_profile_and_child_create_args(monkeypatch, sub_mode):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    adapter = _profile_adapter(monkeypatch)
    base = {"react": {"tool_lazy_load": {"enabled": True, "eager_tools": ["read_file"]}}}
    await adapter.create_instance(sub_mode=sub_mode, config_base=base)
    facade = object.__new__(JiuWenSwarm)
    facade._adapter_mode = "code"
    facade._sdk_name = "test"
    facade._session_manager = SimpleNamespace(has_active_tasks=lambda: False)
    monkeypatch.setattr(facade, "_ensure_adapter", lambda **kwargs: adapter)
    monkeypatch.setattr(adapter, "try_start_dreaming", AsyncMock())
    # Covers the real no-argument call used by skill/plugin installation.
    for _ in range(2):
        await facade.create_instance()
        await asyncio.sleep(0)
        assert adapter._session_instance_sub_mode == sub_mode
        _, rails = _tool_policy_rails(monkeypatch, base["react"], sub_mode, adapter)
        assert sum(isinstance(rail, ProgressiveToolRail) for rail in rails) == int(sub_mode in SINGLE_SUB_MODES)

    child = SimpleNamespace(create_instance=AsyncMock(side_effect=RuntimeError("captured child create")))
    monkeypatch.setattr(adapter, "_new_session_scoped_adapter", lambda *args, **kwargs: child)
    with pytest.raises(RuntimeError, match="captured child create"):
        await adapter._get_or_create_session_adapter("after-rebuild")
    assert child.create_instance.call_args.kwargs["sub_mode"] == sub_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", ["normal", "plan", "team"])
async def test_rebuild_passes_retained_profile_to_initial_rail_plan(monkeypatch, sub_mode):
    adapter = _profile_adapter(monkeypatch)
    await adapter.create_instance(sub_mode=sub_mode, config_base={"react": {}})
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: False)
    monkeypatch.setattr(adapter, "_create_model", lambda *args: object())
    monkeypatch.setattr(adapter, "_get_tool_cards", AsyncMock(return_value=[]))
    assembly = MagicMock(side_effect=RuntimeError("captured rail assembly"))
    monkeypatch.setattr(adapter, "_build_agent_rails", assembly)
    with pytest.raises(RuntimeError, match="captured rail assembly"):
        await adapter.create_instance(config_base={"react": {}})
    assert adapter._session_instance_sub_mode == sub_mode
    assert assembly.call_args.kwargs["sub_mode"] == sub_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", ["normal", "plan", "", " \t "])
async def test_explicit_profile_overrides_saved_team_on_rebuild(monkeypatch, sub_mode):
    adapter = _profile_adapter(monkeypatch)
    await adapter.create_instance(sub_mode="team", config_base={"react": {}})
    await adapter.create_instance(sub_mode=sub_mode, config_base={"react": {}})
    assert adapter._session_instance_sub_mode == sub_mode
    _, rails = _tool_policy_rails(monkeypatch, {"tool_lazy_load": {"enabled": True}}, sub_mode, adapter)
    assert sum(isinstance(rail, ProgressiveToolRail) for rail in rails) == 1


@pytest.mark.parametrize("requested_mode", ["normal", "plan"])
def test_initial_rail_plan_uses_explicit_profile_when_saved_profile_differs(monkeypatch, requested_mode):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._session_instance_sub_mode = "team"
    _, rails = _tool_policy_rails(
        monkeypatch, {"tool_lazy_load": {"enabled": True}}, requested_mode, adapter,
    )
    assert sum(isinstance(rail, ProgressiveToolRail) for rail in rails) == 1
    assert adapter._session_instance_sub_mode == "team"


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_mode", SINGLE_SUB_MODES + TEAM_SUB_MODES)
@pytest.mark.parametrize("enabled", [False, True])
async def test_create_instance_records_profile_and_builds_matching_policy(
    monkeypatch, caplog, sub_mode, enabled,
):
    config = {"tool_lazy_load": {"enabled": enabled, "enable_for_models": ["glm"]}}
    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    # Exercise the real create-instance state setup without starting services.
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: True)
    await adapter.create_instance(mode="code", sub_mode=sub_mode, config_base={"react": config})
    assert adapter._session_instance_sub_mode == sub_mode
    assert adapter._instance is None

    with caplog.at_level(logging.INFO):
        adapter, rails = _tool_policy_rails(monkeypatch, config, sub_mode, adapter)
    single = sub_mode in SINGLE_SUB_MODES
    assert sum(isinstance(rail, ProgressiveToolRail) for rail in rails) == int(single)
    messages = [
        record.getMessage()
        for record in caplog.records
        if "ProgressiveToolRail mounted" in record.getMessage()
    ]
    if single:
        assert adapter._progressive_tool_rail.enabled is enabled
        assert len(messages) == 1
        assert "[JiuwenSwarmCodeAdapter]" in messages[0]
        assert f"mode={'progressive' if enabled else 'skill-gate-only'}" in messages[0]
        assert f"sub_mode={'plan' if sub_mode == 'plan' else 'normal'}" in messages[0]
    else:
        assert adapter._progressive_tool_rail is None
        assert messages == []


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
@pytest.mark.parametrize("sub_mode", TEAM_SUB_MODES)
def test_team_reload_plan_does_not_mount_progressive_rail(
    monkeypatch, enabled, has_stale_rail, sub_mode,
):
    config = (
        {"tool_lazy_load": {"enabled": enabled, "enable_for_models": ["glm"]}}
        if enabled is not None else {}
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._session_instance_sub_mode = sub_mode
    adapter._instance = SimpleNamespace()
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
@pytest.mark.parametrize("enabled", [None, False, True])
@pytest.mark.parametrize("sub_mode", SINGLE_SUB_MODES + TEAM_SUB_MODES)
async def test_config_reload_matches_profile_and_cleans_historical_team_rail(
    monkeypatch, has_stale_rail, enabled, sub_mode,
):
    config = (
        {} if enabled is None else
        {"tool_lazy_load": {"enabled": enabled, "enable_for_models": ["glm"],
                            "eager_tools": ["read_file"]}}
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._session_instance_sub_mode = sub_mode
    adapter._code_agent_rail = None
    adapter._code_plan_approval_rail = None
    instance = SimpleNamespace(
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
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._restore_omitted_reload_fields",
        MagicMock(return_value=None),
    )
    for name in (
        "_build_skill_rail", "_build_skill_credential_injection_rail",
        "_build_skill_active_state_rail", "_update_permission_rail",
        "_sync_multimodal_tools_for_runtime", "_sync_paid_search_tool_for_runtime",
        "_sync_symphony_tools_for_runtime", "_sync_skill_retrieval_tools_for_runtime",
        "_commit_reload_fingerprints",
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

    single = sub_mode in SINGLE_SUB_MODES
    # A second reload must preserve the profile, including after stale cleanup.
    for _ in range(2):
        result = await adapter.reload_agent_config({"react": config})
        assert result.applied is True
        rails = instance.configure.call_args.args[0].rails
        policies = [rail for rail in rails if isinstance(rail, ProgressiveToolRail)]
        if single:
            if enabled is None:
                assert adapter._progressive_tool_rail is old
                assert policies == []
                assert config == {}  # Partial reload input remains untouched.
            else:
                assert policies == [adapter._progressive_tool_rail]
                assert adapter._progressive_tool_rail is not old
                assert adapter._progressive_tool_rail.enabled is enabled
                assert set(CODE_TOOLS) <= set(adapter._progressive_tool_rail.eager_tools)
        else:
            assert adapter._progressive_tool_rail is None
            assert policies == []
        if not single:
            cards = [_card(name) for name in ("read_file", "send_message", "web_fetch_webpage")]
            ctx = SimpleNamespace(inputs=SimpleNamespace(tools=list(cards)))
            for rail in rails:
                await rail.before_model_call(ctx)
            assert ctx.inputs.tools == cards
        assert adapter._session_instance_sub_mode == sub_mode
    if old is not None and not single:
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
