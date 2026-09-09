# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _ContextEngineModelState,
    _deep_agent_context_engine_config,
    _deep_agent_context_engine_config_for_model,
    build_model_from_entry,
)


def test_deep_agent_context_engine_config_forwards_context_window_tokens():
    config = _deep_agent_context_engine_config(
        {"context_engine_config": {"context_window_tokens": "123456"}}
    )

    assert config.context_window_tokens == 123456


def test_deep_agent_context_engine_config_ignores_invalid_context_window_tokens():
    config = _deep_agent_context_engine_config(
        {"context_engine_config": {"context_window_tokens": "not-a-number"}}
    )

    assert config.context_window_tokens is None


def test_deep_agent_context_engine_config_tracks_selected_model_identity():
    model = build_model_from_entry(
        {
            "model_name": "selected-model",
            "client_provider": "OpenAI",
            "api_base": "test-endpoint",
            "api_key": "test-key",
        },
        {},
    )

    config = _deep_agent_context_engine_config_for_model(
        {
            "context_engine_config": {
                "model_name": "startup-model",
                "model_provider": "startup-provider",
            }
        },
        model_state=_ContextEngineModelState(
            model_name="selected-model",
            model=model,
        ),
    )

    assert config.model_name == "selected-model"
    assert config.model_provider == "OpenAI"


def test_selected_model_does_not_keep_startup_exact_tokenizer_spec():
    model = build_model_from_entry(
        {
            "model_name": "selected-model",
            "client_provider": "OpenAI",
            "api_base": "test-endpoint",
            "api_key": "test-key",
        },
        {},
    )

    config = _deep_agent_context_engine_config_for_model(
        {
            "context_engine_config": {
                "tokenizer_spec": {
                    "provider": "OpenAI",
                    "model": "startup-model",
                    "source": "local",
                    "id": "/tmp/startup-tokenizer.json",
                },
                "tokenizer_registry": [
                    {
                        "provider": "OpenAI",
                        "model": "selected-model",
                        "source": "local",
                        "id": "/tmp/selected-tokenizer.json",
                    }
                ],
            }
        },
        model_state=_ContextEngineModelState(
            model_name="selected-model",
            model=model,
        ),
    )

    assert config.tokenizer_spec is None
    assert {spec.model for spec in config.tokenizer_registry} == {
        "startup-model",
        "selected-model",
    }


def test_deep_agent_context_engine_config_forwards_tokenizer_policy(tmp_path):
    config = _deep_agent_context_engine_config(
        {
            "context_engine_config": {
                "enable_tiktoken_counter": True,
                "tokenizer_cache_dir": str(tmp_path / "tokenizers"),
                "tokenizer_offline": True,
                "tokenizer_registry": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "source": "local",
                        "id": str(tmp_path / "tokenizer.json"),
                    }
                ],
            }
        }
    )

    assert config.enable_tiktoken_counter is True
    assert config.enable_tokenizer_download is False
    assert config.tokenizer_cache_dir == str(tmp_path / "tokenizers")
    assert config.tokenizer_offline is True
    assert config.tokenizer_registry[0].provider == "deepseek"


@pytest.mark.asyncio
async def test_code_adapter_forwards_context_window_tokens(tmp_path, monkeypatch):
    config_base = {
        "react": {
            "context_engine_config": {
                "context_window_tokens": "123456",
            },
        },
    }
    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(interface_code, "get_agent_workspace_dir", lambda: tmp_path)

    created_instance = MagicMock(ensure_initialized=AsyncMock())
    created_instance.deep_config = SimpleNamespace(tool_owner_id=None)
    created_instance.configured_rails.return_value = []
    spec = MagicMock()
    spec.build.return_value = created_instance
    captured: dict = {}

    def build_spec(**kwargs):
        captured.update(kwargs)
        context = SimpleNamespace(
            tool_owner_id="code-window-test",
            artifacts=SimpleNamespace(tools=[]),
        )
        return spec, context

    adapter = JiuwenSwarmCodeAdapter()

    def create_sys_operation():
        adapter._sys_operation_card = MagicMock()
        return MagicMock()

    with (
        patch.object(adapter, "set_checkpoint", AsyncMock()),
        patch.object(adapter, "_skip_own_instance_build", return_value=False),
        patch.object(adapter, "_refresh_multimodal_configs"),
        patch.object(adapter, "_create_model", return_value=object()),
        patch.object(adapter, "_build_agent_rails", return_value=[]),
        patch.object(
            adapter, "_create_sys_operation", side_effect=create_sys_operation
        ),
        patch.object(
            adapter, "_build_configured_subagents", return_value=(None, False)
        ),
        patch.object(adapter, "_seed_runtime_cwd"),
        patch.object(adapter, "_register_mcp_servers_from_config", AsyncMock()),
        patch.object(adapter, "load_user_rails", AsyncMock()),
        patch.object(adapter, "_load_active_packages", AsyncMock()),
        patch.object(
            interface_code.code_agent_spec,
            "convert_code_config_to_deep_agent_spec",
            side_effect=build_spec,
        ),
    ):
        await adapter.create_instance()

    context_config = captured["context_engine_config"]
    assert context_config.context_window_tokens == 123456


@pytest.mark.asyncio
async def test_code_adapter_forwards_model_tokenizer_spec_to_context(tmp_path, monkeypatch):
    config_base = {
        "react": {
            "context_engine_config": {
                "enable_tiktoken_counter": True,
            },
        },
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "client_provider": "DeepSeek",
                        "model_name": "deepseek-chat",
                    },
                    "tokenizer_spec": {
                        "source": "local",
                        "id": str(tmp_path / "tokenizer.json"),
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(interface_code, "get_agent_workspace_dir", lambda: tmp_path)

    created_instance = MagicMock(ensure_initialized=AsyncMock())
    created_instance.deep_config = SimpleNamespace(tool_owner_id=None)
    created_instance.configured_rails.return_value = []
    spec = MagicMock()
    spec.build.return_value = created_instance
    captured: dict = {}

    def build_spec(**kwargs):
        captured.update(kwargs)
        context = SimpleNamespace(
            tool_owner_id="code-tokenizer-test",
            artifacts=SimpleNamespace(tools=[]),
        )
        return spec, context

    adapter = JiuwenSwarmCodeAdapter()

    def create_sys_operation():
        adapter._sys_operation_card = MagicMock()
        return MagicMock()

    with (
        patch.object(adapter, "set_checkpoint", AsyncMock()),
        patch.object(adapter, "_skip_own_instance_build", return_value=False),
        patch.object(adapter, "_refresh_multimodal_configs"),
        patch.object(adapter, "_create_model", return_value=object()),
        patch.object(adapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(adapter, "_build_agent_rails", return_value=[]),
        patch.object(
            adapter, "_create_sys_operation", side_effect=create_sys_operation
        ),
        patch.object(adapter, "_build_configured_subagents", return_value=(None, False)),
        patch.object(adapter, "_seed_runtime_cwd"),
        patch.object(adapter, "_register_mcp_servers_from_config", AsyncMock()),
        patch.object(adapter, "load_user_rails", AsyncMock()),
        patch.object(adapter, "_load_active_packages", AsyncMock()),
        patch.object(
            interface_code.code_agent_spec,
            "convert_code_config_to_deep_agent_spec",
            side_effect=build_spec,
        ),
    ):
        await adapter.create_instance()

    context_config = captured["context_engine_config"]
    assert context_config.tokenizer_registry[0].model == "deepseek-chat"
    assert context_config.tokenizer_registry[0].tokenizer_id == str(tmp_path / "tokenizer.json")
    assert context_config.enable_tokenizer_download is False
    assert context_config.tokenizer_offline is True
