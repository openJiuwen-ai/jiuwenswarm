# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


class _FakeDeepAgent:
    def __init__(self, rails: list[object]) -> None:
        self.rails = rails
        self._registered_rails: list[object] = []
        self.ensure_initialized = AsyncMock()
        self.unregistered: list[object] = []
        self.registered: list[object] = []
        self.deep_config = _FakeDeepConfig(marker="old")

    def configured_rails(self) -> list[object]:
        return list(self.rails)

    async def unregister_rail(self, rail: object) -> None:
        self.unregistered.append(rail)
        self.rails = [item for item in self.rails if item is not rail]

    async def register_rail(self, rail: object) -> None:
        self.registered.append(rail)
        if rail not in self.rails:
            self.rails.append(rail)

    def configure(self, config) -> None:
        self.deep_config = config


class _FakeDeepConfig(SimpleNamespace):
    def model_copy(self, *, deep: bool = False):
        del deep
        return _FakeDeepConfig(**vars(self))


@pytest.mark.asyncio
async def test_code_reload_resolves_and_applies_spec_without_replacing_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    old_spec_rail = object()
    external_rail = object()
    new_spec_rail = object()
    instance = _FakeDeepAgent([old_spec_rail, external_rail])

    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = instance
    adapter._code_spec_rails = [old_spec_rail]
    adapter._retired_spec_rail = old_spec_rail
    adapter._config_cache = {"agent_name": "reloaded", "max_iterations": 9}
    adapter._config_base_cache = {"react": {"agent_name": "old"}}
    template_record = object()
    plugin_record = object()
    adapter._loaded_agent_template = ("template-a", template_record, "1")
    adapter._loaded_plugins = {"plugin-a": (plugin_record, "1")}

    parts = SimpleNamespace(
        config=SimpleNamespace(tool_owner_id=None, rails=None),
        rails=[new_spec_rail],
    )
    spec = MagicMock()
    spec.resolve_parts.return_value = parts
    context = SimpleNamespace(
        tool_owner_id="code-spec-owner",
        artifacts=SimpleNamespace(tools=[]),
    )
    config_base = {"react": dict(adapter._config_cache)}

    monkeypatch.setattr(
        adapter,
        "_apply_reload_config_snapshot",
        AsyncMock(return_value=config_base),
    )
    cached_model = object()
    monkeypatch.setattr(adapter, "_create_model", lambda _config: cached_model)
    monkeypatch.setattr(
        adapter,
        "_build_code_spec_snapshot",
        MagicMock(return_value=(spec, context)),
    )
    monkeypatch.setattr(adapter, "_load_active_packages", AsyncMock())
    monkeypatch.setattr(adapter, "load_user_rails", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_sync_mcp_servers_for_runtime",
        AsyncMock(),
    )
    monkeypatch.setattr(
        adapter,
        "_fan_out_reload_to_session_adapters",
        AsyncMock(),
    )
    monkeypatch.setattr(
        adapter,
        "_handle_memory_rail_by_config",
        AsyncMock(),
    )
    monkeypatch.setattr(
        adapter,
        "_sync_active_evolution_review_agent_after_reload",
        MagicMock(),
    )
    sync_multimodal = MagicMock()
    sync_paid_search = MagicMock()
    sync_symphony = MagicMock()
    sync_skill_retrieval = MagicMock()
    sync_skill_prompt = AsyncMock()
    monkeypatch.setattr(adapter, "_sync_multimodal_tools_for_runtime", sync_multimodal)
    monkeypatch.setattr(adapter, "_sync_paid_search_tool_for_runtime", sync_paid_search)
    monkeypatch.setattr(adapter, "_sync_symphony_tools_for_runtime", sync_symphony)
    monkeypatch.setattr(
        adapter,
        "_sync_skill_retrieval_tools_for_runtime",
        sync_skill_retrieval,
    )
    monkeypatch.setattr(
        adapter,
        "_sync_skill_retrieval_prompt_rail_for_runtime",
        sync_skill_prompt,
    )

    def apply_parts(agent, resolved_parts) -> None:
        assert agent is instance
        assert resolved_parts is parts
        assert resolved_parts.config.tool_owner_id == "code-spec-owner"
        assert resolved_parts.config.rails == []
        assert resolved_parts.config.model is cached_model
        instance.rails.append(new_spec_rail)

    monkeypatch.setattr(interface_code, "apply_deep_agent_parts", apply_parts)

    await adapter.reload_agent_config(config_base, {})

    assert adapter._instance is instance
    assert adapter._code_agent_spec is spec
    assert adapter._code_build_context is context
    assert adapter._code_spec_rails == [new_spec_rail]
    assert instance.unregistered == [old_spec_rail]
    assert instance.configured_rails() == [external_rail, new_spec_rail]
    assert adapter._retired_spec_rail is None
    assert adapter._loaded_agent_template == (
        "template-a",
        template_record,
        "1",
    )
    assert adapter._loaded_plugins == {"plugin-a": (plugin_record, "1")}
    spec.resolve_parts.assert_called_once_with(context)
    instance.ensure_initialized.assert_awaited_once()
    sync_multimodal.assert_not_called()
    sync_paid_search.assert_called_once_with()
    sync_symphony.assert_called_once_with(config_base)
    sync_skill_retrieval.assert_called_once_with(config_base)
    sync_skill_prompt.assert_awaited_once_with(config_base)


@pytest.mark.asyncio
async def test_code_multimodal_reload_preserves_snapshot_without_registering_tools(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = _FakeDeepAgent([])
    adapter._vision_model_config = object()
    adapter._audio_model_config = object()
    adapter._video_model_config = True
    adapter._image_gen_model_config = True
    config_base = {"react": {"agent_name": "code"}}
    env_overrides = {}
    refresh = AsyncMock(return_value=config_base)
    fan_out = AsyncMock()
    sync_group = MagicMock()
    monkeypatch.setattr(adapter, "_apply_multimodal_reload_snapshot", refresh)
    monkeypatch.setattr(adapter, "_fan_out_reload_to_session_adapters", fan_out)
    monkeypatch.setattr(adapter, "_sync_tool_group", sync_group)

    await adapter.reload_agent_config(
        config_base, env_overrides, reload_scopes={"multimodal"}
    )

    refresh.assert_awaited_once_with(config_base, env_overrides)
    fan_out.assert_awaited_once_with(
        config_base,
        env_overrides,
        None,
        {"multimodal"},
        permission_notification=False,
    )
    sync_group.assert_not_called()
    assert not adapter._vision_tools
    assert not adapter._audio_tools


@pytest.mark.asyncio
async def test_code_reload_rolls_back_spec_rails_when_apply_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    old_spec_rail = object()
    external_rail = object()
    candidate_rail = object()
    instance = _FakeDeepAgent([old_spec_rail, external_rail])

    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = instance
    old_spec = object()
    old_context = SimpleNamespace(
        tool_owner_id="old-owner",
        artifacts=SimpleNamespace(tools=[]),
    )
    adapter._code_agent_spec = old_spec
    adapter._code_build_context = old_context
    adapter._code_spec_rails = [old_spec_rail]
    adapter._config_cache = {"agent_name": "old"}
    adapter._config_base_cache = {"react": {"agent_name": "old"}}
    old_model = object()
    old_model_client_config = object()
    old_model_request_config = object()
    adapter._model = old_model
    adapter._model_client_config = old_model_client_config
    adapter._model_request_config = old_model_request_config
    adapter._default_model_name = "old-model"
    adapter._last_models_config_fingerprint = "old-fingerprint"
    adapter._model_cache = {"old-model": old_model}
    adapter._model_name_to_keys = {"old-model": ["old-model"]}
    template_state = ("template-a", object(), "1")
    plugin_state = {"plugin-a": (object(), "1")}
    adapter._loaded_agent_template = template_state
    adapter._loaded_plugins = plugin_state.copy()

    config_base = {"react": {"agent_name": "new"}}
    parts = SimpleNamespace(
        config=SimpleNamespace(tool_owner_id=None, rails=None),
        rails=[candidate_rail],
    )
    new_spec = MagicMock()
    new_spec.resolve_parts.return_value = parts
    new_context = SimpleNamespace(
        tool_owner_id="new-owner",
        artifacts=SimpleNamespace(tools=[]),
    )

    monkeypatch.setattr(
        adapter,
        "_apply_reload_config_snapshot",
        AsyncMock(return_value=config_base),
    )
    new_model = object()

    def create_model(_config):
        adapter._model = new_model
        adapter._model_client_config = object()
        adapter._model_request_config = object()
        adapter._default_model_name = "new-model"
        adapter._last_models_config_fingerprint = "new-fingerprint"
        adapter._model_cache = {"new-model": new_model}
        adapter._model_name_to_keys = {"new-model": ["new-model"]}
        return new_model

    monkeypatch.setattr(adapter, "_create_model", create_model)
    monkeypatch.setattr(
        adapter,
        "_build_code_spec_snapshot",
        MagicMock(return_value=(new_spec, new_context)),
    )
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    monkeypatch.setattr(
        interface_code,
        "apply_deep_agent_parts",
        MagicMock(side_effect=RuntimeError("apply failed")),
    )

    with pytest.raises(RuntimeError, match="apply failed"):
        await adapter.reload_agent_config(config_base, {})

    assert adapter._code_agent_spec is old_spec
    assert adapter._code_build_context is old_context
    assert adapter._code_spec_rails == [old_spec_rail]
    assert instance.configured_rails() == [external_rail, old_spec_rail]
    assert instance.registered == [old_spec_rail]
    assert adapter._loaded_agent_template is template_state
    assert adapter._loaded_plugins == plugin_state
    assert adapter._agent_name == "main_agent"
    assert adapter._model is old_model
    assert adapter._model_client_config is old_model_client_config
    assert adapter._model_request_config is old_model_request_config
    assert adapter._default_model_name == "old-model"
    assert adapter._last_models_config_fingerprint == "old-fingerprint"
    assert adapter._model_cache == {"old-model": old_model}
    assert adapter._model_name_to_keys == {"old-model": ["old-model"]}


@pytest.mark.asyncio
async def test_code_reload_validation_failure_does_not_reregister_active_tools(
    monkeypatch: pytest.MonkeyPatch,
):
    instance = _FakeDeepAgent([])
    old_tool = object()
    old_context = SimpleNamespace(
        tool_owner_id="old-owner",
        artifacts=SimpleNamespace(tools=[old_tool]),
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = instance
    adapter._code_build_context = old_context
    adapter._config_cache = {"agent_name": "old", "max_iterations": 7}
    adapter._config_base_cache = {"react": dict(adapter._config_cache)}

    config_base = {
        "react": {"agent_name": "invalid", "max_iterations": "not-an-int"}
    }
    monkeypatch.setattr(
        adapter,
        "_apply_reload_config_snapshot",
        AsyncMock(return_value=config_base),
    )
    monkeypatch.setattr(adapter, "_create_model", lambda _config: object())
    monkeypatch.setattr(
        adapter,
        "_build_code_spec_snapshot",
        MagicMock(side_effect=ValueError("invalid max_iterations")),
    )
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    register_tool_mock = MagicMock()
    monkeypatch.setattr(interface_code, "register_tool", register_tool_mock)

    with pytest.raises(ValueError, match="invalid max_iterations"):
        await adapter.reload_agent_config(config_base, {})

    register_tool_mock.assert_not_called()
    assert adapter._code_build_context is old_context
    assert adapter._config_cache == {"agent_name": "old", "max_iterations": 7}


@pytest.mark.asyncio
async def test_config_reload_does_not_replace_caller_supplied_spec(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = JiuwenSwarmCodeAdapter()
    custom_spec = object()
    custom_context = object()
    instance = object()
    adapter._custom_code_spec_active = True
    adapter._code_agent_spec = custom_spec
    adapter._code_build_context = custom_context
    adapter._instance = instance
    config_base = {"react": {"agent_name": "config-reload"}}
    apply_snapshot = AsyncMock(return_value=config_base)
    fan_out = AsyncMock()
    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", apply_snapshot)
    monkeypatch.setattr(adapter, "_fan_out_reload_to_session_adapters", fan_out)
    build_snapshot = MagicMock()
    monkeypatch.setattr(adapter, "_build_code_spec_snapshot", build_snapshot)

    await adapter.reload_agent_config(config_base, {"API_KEY": "updated"})

    apply_snapshot.assert_awaited_once_with(
        config_base,
        {"API_KEY": "updated"},
    )
    fan_out.assert_awaited_once_with(
        config_base,
        {"API_KEY": "updated"},
        None,
    )
    build_snapshot.assert_not_called()
    assert adapter._instance is instance
    assert adapter._code_agent_spec is custom_spec
    assert adapter._code_build_context is custom_context
