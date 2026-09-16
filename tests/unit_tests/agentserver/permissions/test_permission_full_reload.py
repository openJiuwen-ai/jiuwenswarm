"""Full Deep reload keeps real SDK configuration behind Smart admission."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import DeepAgentConfig

from tests.unit_tests.agentserver.permissions.test_permission_lifecycle_integration import (
    lifecycle as lifecycle,
)


class _ReloadRail(AgentRail):
    def __init__(self):
        super().__init__()
        self.initialized = 0

    def init(self, _agent):
        self.initialized += 1


def _isolate_optional_providers(monkeypatch, h):
    """Stub external providers, never the Host reload or SDK lifecycle."""
    adapter = h.adapter
    rail = _ReloadRail()

    async def snapshot(config, _env):
        adapter._config_base_cache = deepcopy(config)
        adapter._config_cache = deepcopy(config.get("react", {}))
        return config

    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", snapshot)
    for name in (
        "_cancel_skill_retrieval_build_after_reload", "_try_init_a2x_client",
        "_sync_skill_retrieval_prompt_rail_for_runtime", "load_user_rails",
        "_sync_mcp_servers_for_runtime", "_load_active_packages", "_handle_memory_rail_by_config",
    ):
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in (
        "_sync_a2x_runtime_state", "_sync_multimodal_tools_for_runtime",
        "_sync_paid_search_tool_for_runtime", "_sync_symphony_tools_for_runtime",
        "_sync_skill_retrieval_tools_for_runtime", "_sync_active_evolution_review_agent_after_reload",
    ):
        monkeypatch.setattr(adapter, name, Mock())
    monkeypatch.setattr(adapter, "_create_model", Mock(return_value=adapter._model))
    monkeypatch.setattr(adapter, "_get_current_agent_rails", Mock(return_value=[rail]))
    monkeypatch.setattr(adapter, "_make_deep_agent_config", lambda **kw: DeepAgentConfig(
        rails=kw["rails"], auto_create_workspace=False,
    ))
    return rail


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "configure", "ensure_initialized"])
async def test_full_reload_configure_is_admitted_only_after_real_initialization(lifecycle, monkeypatch, failure):
    h = lifecycle
    h.change()
    await h.reload()
    old_permission = h.adapter._permission_rail
    added = _isolate_optional_providers(monkeypatch, h)
    h.desired["react"] = {"agent_name": "updated-name"}
    configured = Mock(wraps=h.instance.configure)
    monkeypatch.setattr(h.instance, "configure", configured)
    reached, release = asyncio.Event(), asyncio.Event()
    if failure == "configure":
        def configure_then_fail(config):
            configured._mock_wraps(config)
            raise RuntimeError("configure failed after mutation")
        configured.side_effect = configure_then_fail
    initialize = h.instance.ensure_initialized

    async def initialize_then_pause():
        await initialize()
        reached.set()
        await release.wait()
        if failure == "ensure_initialized":
            raise RuntimeError("initialize failed after registration")

    monkeypatch.setattr(h.instance, "ensure_initialized", initialize_then_pause)
    task = asyncio.create_task(h.adapter.reload_agent_config(h.desired, reload_scopes={"models"}))
    requesting = None
    try:
        if failure != "configure":
            await asyncio.wait_for(reached.wait(), 5)
            requesting = asyncio.create_task(h.request())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert h.observed == [] and not requesting.done()
            release.set()
        if failure:
            with pytest.raises(RuntimeError, match="failed after"):
                await asyncio.wait_for(task, 5)
            if requesting:
                with pytest.raises(RuntimeError, match="permission_session_isolated"):
                    await requesting
            assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
            assert h.instance.configured_rails() == [] and h.callbacks() == []
        else:
            await asyncio.wait_for(task, 5)
            await asyncio.wait_for(requesting, 5)
            assert h.adapter._permission_rail is not old_permission
            assert h.instance.is_registered_rail(added) and added.initialized == 1
            assert h.permissions() == [h.adapter._permission_rail]
            assert len(h.observed) == 1
        configured.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(task, *([requesting] if requesting else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_full_reload_busy_does_not_call_any_live_provider(lifecycle, monkeypatch):
    h = lifecycle
    h.change()
    await h.reload()
    _isolate_optional_providers(monkeypatch, h)
    h.desired["react"] = {"agent_name": "updated-name"}
    h.adapter._mark_session_active("lifecycle")
    try:
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.adapter.reload_agent_config(h.desired, reload_scopes={"models"})
        h.adapter._create_model.assert_not_called()
        h.adapter._sync_mcp_servers_for_runtime.assert_not_awaited()
        assert not h.adapter._permission_state.permission_isolated
    finally:
        h.adapter._unmark_session_active("lifecycle")
