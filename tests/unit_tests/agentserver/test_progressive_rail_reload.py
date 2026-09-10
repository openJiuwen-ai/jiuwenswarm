# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rail lifecycle regression tests for DeepAgent config reloads."""

# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _rail_plan_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._build_skill_rail = MagicMock(return_value=None)
    adapter._build_progressive_tool_rail = MagicMock(return_value=None)
    adapter._build_skill_credential_injection_rail = MagicMock(return_value=None)
    adapter._build_skill_active_state_rail = MagicMock(return_value=None)
    adapter._filesystem_rail_enabled_for_profile = MagicMock(return_value=True)
    adapter._update_permission_rail = MagicMock()
    return adapter


def test_reload_plan_preserves_progressive_rail_when_config_omits_tool_lazy_load():
    adapter = _rail_plan_adapter()
    old_rail = MagicMock(name="old-progressive-tool-rail")
    adapter._progressive_tool_rail = old_rail

    rails, rails_to_unregister = adapter._get_current_agent_rails({}, {"react": {}})

    adapter._build_progressive_tool_rail.assert_not_called()
    assert old_rail not in rails
    assert rails_to_unregister == []
    assert adapter._progressive_tool_rail is old_rail


def test_reload_plan_does_not_create_progressive_rail_when_config_omits_tool_lazy_load():
    adapter = _rail_plan_adapter()

    rails, rails_to_unregister = adapter._get_current_agent_rails({}, {"react": {}})

    adapter._build_progressive_tool_rail.assert_not_called()
    assert rails == []
    assert rails_to_unregister == []
    assert adapter._progressive_tool_rail is None


def test_reload_plan_stages_progressive_rail_when_disabled():
    adapter = _rail_plan_adapter()
    old_rail = MagicMock(name="old-progressive-tool-rail")
    adapter._progressive_tool_rail = old_rail

    rails, rails_to_unregister = adapter._get_current_agent_rails(
        {"tool_lazy_load": {"enabled": False}},
        {"react": {"tool_lazy_load": {"enabled": False}}},
    )

    assert old_rail not in rails
    assert rails_to_unregister == [old_rail]
    assert adapter._progressive_tool_rail is old_rail


def test_reload_plan_adds_new_progressive_rail_when_enabled():
    adapter = _rail_plan_adapter()
    new_rail = MagicMock(name="new-progressive-tool-rail")
    adapter._build_progressive_tool_rail.return_value = new_rail

    rails, rails_to_unregister = adapter._get_current_agent_rails(
        {"tool_lazy_load": {"enabled": True}},
        {"react": {"tool_lazy_load": {"enabled": True}}},
    )

    assert rails_to_unregister == []
    assert new_rail in rails
    assert adapter._progressive_tool_rail is new_rail


def test_reload_plan_replaces_disabled_tools_rail():
    adapter = _rail_plan_adapter()
    old_rail = MagicMock(name="old-disabled-tools-rail")
    new_rail = MagicMock(name="new-disabled-tools-rail")
    adapter._disabled_tools_rail = old_rail
    adapter._build_disabled_tools_rail = MagicMock(return_value=new_rail)

    rails, rails_to_unregister = adapter._get_current_agent_rails(
        {"disabled_tools": ["search_skill"]},
        {"react": {"disabled_tools": ["search_skill"]}},
    )

    assert new_rail in rails
    assert old_rail not in rails
    assert rails_to_unregister == [old_rail]
    assert adapter._disabled_tools_rail is new_rail


def test_reload_plan_retires_disabled_tools_rail_when_blacklist_is_cleared():
    adapter = _rail_plan_adapter()
    old_rail = MagicMock(name="old-disabled-tools-rail")
    adapter._disabled_tools_rail = old_rail

    rails, rails_to_unregister = adapter._get_current_agent_rails(
        {"disabled_tools": []},
        {"react": {"disabled_tools": []}},
    )

    assert old_rail not in rails
    assert rails_to_unregister == [old_rail]
    assert adapter._disabled_tools_rail is None

    # A later partial reload that omits disabled_tools must not resurrect the
    # retired rail from a stale adapter attribute.
    rails, rails_to_unregister = adapter._get_current_agent_rails(
        {}, {"react": {}}
    )

    assert old_rail not in rails
    assert rails_to_unregister == []
    assert adapter._disabled_tools_rail is None


def test_reload_plan_stages_filesystem_rail_when_profile_disables_it():
    adapter = _rail_plan_adapter()
    old_rail = MagicMock(name="old-filesystem-rail")
    adapter._filesystem_rail = old_rail
    adapter._filesystem_rail_enabled_for_profile.return_value = False

    rails, rails_to_unregister = adapter._get_current_agent_rails({}, {"react": {}})

    assert old_rail not in rails
    assert rails_to_unregister == [old_rail]
    assert adapter._filesystem_rail is old_rail


def test_code_adapter_reload_plan_preserves_parent_retirement_plan(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    base_rail = object()
    retired_rail = object()
    code_rail = object()
    approval_rail = object()
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_agent_rail = code_rail
    adapter._code_plan_approval_rail = approval_rail
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_current_agent_rails",
        lambda _self, _config, _config_base=None: ([base_rail], [retired_rail]),
    )

    rails, rails_to_unregister = adapter._get_current_agent_rails({}, {"react": {}})

    assert rails == [base_rail, code_rail, approval_rail]
    assert rails_to_unregister == [retired_rail]


@pytest.mark.asyncio
async def test_code_adapter_reconcile_only_removes_evolution_rails(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._config_cache = {"evolution": {"enabled": True}}
    adapter._skill_evolution_rail = object()
    adapter._evolution_interrupt_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)

    await adapter._reconcile_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_awaited_once_with()


def _prepare_reload_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configure_error: BaseException | None = None,
) -> tuple[JiuWenSwarmDeepAdapter, list[str], object, object]:
    adapter = JiuWenSwarmDeepAdapter()
    old_progressive = object()
    old_filesystem = object()
    adapter._progressive_tool_rail = old_progressive
    adapter._filesystem_rail = old_filesystem
    adapter._config_base_cache = {"react": {"tool_lazy_load": {"enabled": True}}}
    adapter._config_cache = {"tool_lazy_load": {"enabled": True}}

    events: list[str] = []
    instance = MagicMock()

    def _configure(_config):
        events.append("configure")
        if configure_error is not None:
            raise configure_error

    async def _unregister(rail):
        name = "progressive" if rail is old_progressive else "filesystem"
        events.append(f"unregister:{name}")

    instance.configure.side_effect = _configure
    instance.unregister_rail = AsyncMock(side_effect=_unregister)
    # reload 路径会 await self._instance.ensure_initialized()（interface_deep.py:8307），
    # 裸 MagicMock 的该方法返回不可 await 的对象 → TypeError，故配 AsyncMock。
    instance.ensure_initialized = AsyncMock()
    adapter._instance = instance

    new_config = {
        "react": {
            "agent_name": "main_agent",
            "tool_lazy_load": {"enabled": False},
        }
    }

    async def _apply_snapshot(_config_base, _env_overrides):
        adapter._config_base_cache = new_config.copy()
        adapter._config_cache = new_config["react"].copy()
        return new_config

    async def _reconcile_evolution():
        events.append("reconcile:evolution")

    monkeypatch.setattr(
        adapter, "_bind_request_env_overlay", MagicMock(return_value=(None, None, None))
    )
    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", _apply_snapshot)
    monkeypatch.setattr(adapter, "_create_model", MagicMock(return_value=object()))
    monkeypatch.setattr(adapter, "_sync_multimodal_tools_for_runtime", MagicMock())
    monkeypatch.setattr(adapter, "_sync_paid_search_tool_for_runtime", MagicMock())
    monkeypatch.setattr(adapter, "_sync_symphony_tools_for_runtime", MagicMock())
    monkeypatch.setattr(adapter, "_sync_skill_retrieval_tools_for_runtime", MagicMock())
    monkeypatch.setattr(
        adapter, "_sync_skill_retrieval_prompt_rail_for_runtime", AsyncMock()
    )
    monkeypatch.setattr(
        adapter, "_filesystem_rail_enabled_for_profile", MagicMock(return_value=False)
    )
    monkeypatch.setattr(adapter, "_build_skill_rail", MagicMock(return_value=None))
    monkeypatch.setattr(
        adapter, "_build_progressive_tool_rail", MagicMock(return_value=None)
    )
    monkeypatch.setattr(adapter, "_update_permission_rail", MagicMock())
    monkeypatch.setattr(adapter, "load_user_rails", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_make_deep_agent_config",
        MagicMock(return_value=SimpleNamespace(model=object(), system_prompt="prompt")),
    )
    monkeypatch.setattr(
        adapter, "_omit_unchanged_reload_fields", MagicMock(return_value=({}, {}))
    )
    monkeypatch.setattr(adapter, "_restore_omitted_reload_fields", MagicMock())
    monkeypatch.setattr(adapter, "_commit_reload_fingerprints", MagicMock())
    monkeypatch.setattr(
        adapter, "_sync_active_evolution_review_agent_after_reload", MagicMock()
    )
    monkeypatch.setattr(adapter, "_sync_mcp_servers_for_runtime", AsyncMock())
    monkeypatch.setattr(adapter, "_fan_out_reload_to_session_adapters", AsyncMock())
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", AsyncMock())
    monkeypatch.setattr(
        adapter, "_reconcile_evolution_rails", _reconcile_evolution, raising=False
    )
    return adapter, events, old_progressive, old_filesystem


@pytest.mark.asyncio
async def test_reload_unregisters_retired_rails_only_after_configure(monkeypatch):
    adapter, events, _old_progressive, _old_filesystem = _prepare_reload_adapter(
        monkeypatch
    )

    result = await adapter.reload_agent_config(
        {"react": {"tool_lazy_load": {"enabled": False}}},
        {},
        _force_apply=True,
    )

    assert result.applied is True
    assert events[0] == "configure"
    assert set(events[1:3]) == {"unregister:progressive", "unregister:filesystem"}
    assert events[3] == "reconcile:evolution"
    assert adapter._progressive_tool_rail is None
    assert adapter._filesystem_rail is None


@pytest.mark.asyncio
async def test_reload_does_not_unregister_staged_rails_when_configure_fails(monkeypatch):
    adapter, events, old_progressive, old_filesystem = _prepare_reload_adapter(
        monkeypatch,
        configure_error=RuntimeError("configure failed"),
    )
    previous_base = adapter._config_base_cache
    previous_react = adapter._config_cache

    with pytest.raises(RuntimeError, match="configure failed"):
        await adapter.reload_agent_config(
            {"react": {"tool_lazy_load": {"enabled": False}}},
            {},
            _force_apply=True,
        )

    assert events == ["configure"]
    assert adapter._progressive_tool_rail is old_progressive
    assert adapter._filesystem_rail is old_filesystem
    assert adapter._config_base_cache is not previous_base
    assert adapter._config_cache is not previous_react
    assert adapter._config_cache == {
        "agent_name": "main_agent",
        "tool_lazy_load": {"enabled": False},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "expected_call"),
    [(True, "ensure"), (False, "unconfigure")],
)
async def test_reconcile_evolution_rails_uses_current_mode_owner(
    monkeypatch,
    enabled,
    expected_call,
):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": enabled}}
    if not enabled:
        adapter._skill_evolution_rail = object()
    calls: list[str] = []

    async def _ensure():
        calls.append("ensure")

    async def _unconfigure():
        calls.append("unconfigure")

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", _ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", _unconfigure)

    await adapter._reconcile_evolution_rails()

    assert calls == [expected_call]


@pytest.mark.asyncio
async def test_reconcile_evolution_rails_skips_unconfigure_without_live_rails(
    monkeypatch,
):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": False}}
    unconfigure = AsyncMock()
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)

    await adapter._reconcile_evolution_rails()

    unconfigure.assert_not_awaited()
