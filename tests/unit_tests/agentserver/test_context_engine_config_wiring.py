# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep


def _react_context_config() -> dict:
    return {
        "context_engine_config": {
            "context_window_tokens": 131072,
            "model_name": "Qwen3-8B",
            "model_context_window_tokens": {"Qwen3-8B": 131072},
            "enable_openrouter_model_context_window_tokens": False,
            "openrouter_request_timeout": 1.5,
            "enable_reload": True,
            # Processor configuration belongs to ContextProcessorRail and must
            # not be forwarded as an unknown ContextEngineConfig field.
            "round_level_compressor_config": {
                "trigger_context_ratio": 0.71875,
            },
        }
    }


def test_deep_agent_context_engine_config_preserves_runtime_window_fields():
    config = interface_deep._deep_agent_context_engine_config(
        _react_context_config()
    )

    assert config.context_window_tokens == 131072
    assert config.model_name == "Qwen3-8B"
    assert config.model_context_window_tokens == {"Qwen3-8B": 131072}
    assert config.enable_openrouter_model_context_window_tokens is False
    assert config.openrouter_request_timeout == 1.5
    assert config.enable_reload is True
    assert "round_level_compressor_config" not in config.model_dump()


@pytest.mark.asyncio
async def test_code_adapter_passes_context_engine_config_to_deep_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    react_config = _react_context_config()
    config_base = {
        "preferred_language": "en",
        "react": react_config,
    }
    captured: dict = {}

    async def ensure_initialized() -> None:
        return None

    ability_manager = SimpleNamespace(set_owner_id=lambda _owner_id: None)
    instance = SimpleNamespace(
        ensure_initialized=ensure_initialized,
        _registered_rails=[],
        deep_config=SimpleNamespace(tool_owner_id=None),
        ability_manager=ability_manager,
        configured_rails=lambda: [],
    )
    spec = SimpleNamespace(build=lambda _context: instance)

    def build_spec(**kwargs):
        captured.update(kwargs)
        context = SimpleNamespace(
            tool_owner_id="code-context-test",
            artifacts=SimpleNamespace(tools=[]),
        )
        return spec, context

    adapter = interface_code.JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: False)
    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(
        interface_code,
        "get_agent_workspace_dir",
        lambda: tmp_path / "agent-workspace",
    )
    monkeypatch.setattr(
        interface_code.code_agent_spec,
        "convert_code_config_to_deep_agent_spec",
        build_spec,
    )
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", lambda config: None)
    monkeypatch.setattr(adapter, "_create_model", lambda config: object())
    monkeypatch.setattr(
        adapter,
        "_build_agent_rails",
        lambda config, config_base, mode: [],
    )

    def create_sys_operation():
        adapter._sys_operation_card = SimpleNamespace()
        return object()

    monkeypatch.setattr(adapter, "_create_sys_operation", create_sys_operation)
    monkeypatch.setattr(
        adapter,
        "_build_configured_subagents",
        lambda model, config, config_base: ([], False),
    )
    monkeypatch.setattr(
        adapter,
        "_seed_runtime_cwd",
        lambda project_dir, workspace: None,
    )
    monkeypatch.setattr(
        adapter,
        "_ensure_cron_tools_registered",
        lambda parent_session_id: None,
    )
    monkeypatch.setattr(
        adapter,
        "_register_mcp_servers_from_config",
        AsyncMock(),
    )
    monkeypatch.setattr(adapter, "load_user_rails", AsyncMock())
    monkeypatch.setattr(adapter, "_load_active_packages", AsyncMock())

    await adapter.create_instance({"project_dir": str(tmp_path / "project")})

    context_config = captured["context_engine_config"]
    assert context_config.context_window_tokens == 131072
    assert context_config.model_name == "Qwen3-8B"
    assert context_config.model_context_window_tokens == {"Qwen3-8B": 131072}
