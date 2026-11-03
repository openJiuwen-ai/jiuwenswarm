"""Enterprise sessions reconcile MCPs from the routed Agent's effective policy."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.enterprise.config import config_provider
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _child(servers: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        _instance=object(),
        _config_base_cache={"mcp": {"servers": [{"name": "local-only"}]}},
        _session_selected_mcp=set(),
        _pending_skill_scan_mcp_names=set(),
        _load_enterprise_config=AsyncMock(
            return_value=SimpleNamespace(config={"mcp": {"servers": servers}}),
        ),
        register_mcp_by_name=AsyncMock(),
        unregister_mcp_by_name=AsyncMock(),
        clear_pending_skill_scan_mcp_names=Mock(),
        refresh_skill_rails=AsyncMock(),
        discard_transient_skill_retrieval_session_profile=Mock(),
    )


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_provider, "resolve_config_source",
        lambda: config_provider.ConfigSource.ENTERPRISE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("picker", [None, [], ["unauthorized-picker-entry"]])
async def test_enterprise_policy_loads_without_picker_and_is_idempotent(enterprise, picker):
    child = _child([{"name": "policy-mcp", "enabled": True}, {"name": "disabled", "enabled": False}])
    original_base = child._config_base_cache
    baseline = copy.deepcopy(original_base)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)
    request = SimpleNamespace(channel_id="web", session_id="session-a")

    async def register(name: str) -> None:
        assert name == "policy-mcp"
        assert child._config_base_cache["mcp"]["servers"][0]["name"] == name

    child.register_mcp_by_name.side_effect = register
    await adapter.reconcile_session_mcp("session-a", picker, execution_request=request)
    await adapter.reconcile_session_mcp("session-a", picker, execution_request=request)

    assert child._session_selected_mcp == {"policy-mcp"}
    child.register_mcp_by_name.assert_awaited_once_with("policy-mcp")
    child.unregister_mcp_by_name.assert_not_awaited()
    assert child._load_enterprise_config.await_args.args == (request,)
    assert original_base == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("servers", [[], [{"name": "old-mcp", "enabled": False}]])
async def test_enterprise_removed_or_disabled_template_unloads(enterprise, servers):
    child = _child(servers)
    child._session_selected_mcp.add("old-mcp")
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)

    await adapter.reconcile_session_mcp(
        "session-a", ["old-mcp"], execution_request=SimpleNamespace(channel_id="web"),
    )

    assert child._session_selected_mcp == set()
    child.unregister_mcp_by_name.assert_awaited_once_with("old-mcp")
    child.register_mcp_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_enterprise_agent_sessions_keep_distinct_policies(enterprise):
    children = {"a": _child([{"name": "only-a"}]), "b": _child([])}
    adapter = object.__new__(JiuWenSwarmDeepAdapter)

    async def get_child(session_id: str, **kwargs) -> SimpleNamespace:
        return children[session_id]

    adapter._get_or_create_session_adapter = get_child
    for session_id in children:
        await adapter.reconcile_session_mcp(
            session_id, ["only-a"], execution_request=SimpleNamespace(channel_id="web"),
        )

    assert children["a"]._session_selected_mcp == {"only-a"}
    assert children["b"]._session_selected_mcp == set()
    children["b"].register_mcp_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_config_keeps_per_message_selection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_provider, "resolve_config_source", lambda: None)
    child = _child([{"name": "enterprise-only"}])
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)

    await adapter.reconcile_session_mcp(
        "local", ["picked-mcp"], execution_request=SimpleNamespace(channel_id="web"),
    )
    await adapter.reconcile_session_mcp(
        "local", [], execution_request=SimpleNamespace(channel_id="web"),
    )

    child._load_enterprise_config.assert_not_awaited()
    child.register_mcp_by_name.assert_awaited_once_with("picked-mcp")
    child.unregister_mcp_by_name.assert_awaited_once_with("picked-mcp")
