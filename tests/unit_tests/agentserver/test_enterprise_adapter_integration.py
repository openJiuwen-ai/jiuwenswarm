"""Mainline model preparation and enterprise policy/MCP boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.model_errors import (
    MODEL_SELECTION_FORBIDDEN,
    MODEL_SELECTION_NOT_FOUND,
    ModelSelectionError,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as module


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [MODEL_SELECTION_NOT_FOUND, MODEL_SELECTION_FORBIDDEN])
async def test_enterprise_prewarm_hydrates_missing_catalog_without_hiding_denials(monkeypatch, code):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    adapter = object.__new__(module.JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._config_base_cache = {}
    adapter._permission_state = SimpleNamespace(permission_update_in_progress=False)
    adapter._requested_model_name = lambda request: "model:tenant-model"

    def unavailable(name):
        raise ModelSelectionError(code, "selection unavailable")

    adapter._has_valid_model_config = unavailable
    configured = {"models": {"defaults": [{"model_id": "tenant-model"}]}}
    adapter._load_enterprise_config = AsyncMock(return_value=SimpleNamespace(
        config=configured, policy=SimpleNamespace(models={"main": "tenant-model"}),
    ))
    adapter._update_permission_rail = MagicMock()
    adapter._create_model = MagicMock()
    request = SimpleNamespace()
    if code == MODEL_SELECTION_FORBIDDEN:
        with pytest.raises(ModelSelectionError) as error:
            await adapter._ensure_enterprise_models(request)
        assert error.value.code == code
        adapter._load_enterprise_config.assert_not_awaited()
        adapter._create_model.assert_not_called()
    else:
        await adapter._ensure_enterprise_models(request)
        adapter._load_enterprise_config.assert_awaited_once_with(request, base={})
        adapter._create_model.assert_called_once_with(configured)
        adapter._update_permission_rail.assert_called_once_with(configured)


@pytest.mark.asyncio
async def test_static_mcp_uses_mainline_resource_lifecycle(monkeypatch):
    adapter = object.__new__(module.JiuWenSwarmDeepAdapter)
    adapter._register_static_mcp_server = AsyncMock(return_value=True)
    cfg = object()
    assert await adapter._register_mcp_server(cfg, tag="session")
    adapter._register_static_mcp_server.assert_awaited_once_with(cfg, tag="session")


@pytest.mark.asyncio
@pytest.mark.parametrize("reachable", [True, False])
async def test_dynamic_mcp_token_registration_survives_until_unregister(monkeypatch, reachable):
    from jiuwenswarm.common import mcp_dynamic_token_patch as tokens

    tokens._reset_dynamic_token_state_for_tests()
    adapter = object.__new__(module.JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=MagicMock())
    adapter._registered_mcp_server_ids = set()
    adapter._registered_mcp_servers = {}
    adapter._safe_remove_mcp_server = AsyncMock()
    cfg = SimpleNamespace(server_id="dynamic-tenant", server_name="dynamic",
                          client_type="sse", server_path="http://mcp.invalid")
    preflight = AsyncMock(return_value=(reachable, "probe"))
    monkeypatch.setattr(module, "preflight_mcp_server_reachable", preflight)
    registry = SimpleNamespace(add_mcp_server=AsyncMock(return_value=True))
    monkeypatch.setattr(module, "Runner", SimpleNamespace(resource_mgr=registry))
    try:
        registered = await adapter._register_mcp_server(
            cfg, tag="session", dynamic_token_url="http://token.invalid",
        )
        assert registered is reachable
        preflight.assert_awaited_once_with(cfg, dynamic_token_url="http://token.invalid")
        assert tokens.is_dynamic_token_server(cfg.server_id) is reachable
        if reachable:
            assert adapter._registered_mcp_servers[cfg.server_id] is cfg
            await adapter._unregister_mcp_server(cfg.server_id)
            adapter._safe_remove_mcp_server.assert_awaited_once_with(cfg.server_id)
            assert cfg.server_id not in adapter._registered_mcp_servers
            assert not tokens.is_dynamic_token_server(cfg.server_id)
        else:
            registry.add_mcp_server.assert_not_awaited()
    finally:
        tokens._reset_dynamic_token_state_for_tests()
