"""Enterprise sessions reconcile MCPs from the routed Agent's effective policy."""

from __future__ import annotations

import asyncio
import copy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.enterprise.config import config_provider
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _child(servers: list[object]) -> SimpleNamespace:
    child = SimpleNamespace(
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
    child.load_enterprise_mcp_names = MethodType(
        JiuWenSwarmDeepAdapter.load_enterprise_mcp_names, child,
    )
    return child


@pytest.mark.asyncio
async def test_enterprise_mcp_names_normalize_and_filter_entries():
    child = _child([
        None, "invalid", {}, {"name": "   "}, {"name": None},
        {"name": "disabled", "enabled": False},
        {"name": " policy-mcp "}, {"name": "policy-mcp", "enabled": True},
    ])
    request = SimpleNamespace(channel_id="web")

    assert await child.load_enterprise_mcp_names(request) == {"policy-mcp"}
    child._load_enterprise_config.assert_awaited_once_with(
        request, base={"mcp": {"servers": [{"name": "local-only"}]}},
    )
    assert child._config_base_cache == child._load_enterprise_config.return_value.config
    assert child._config_base_cache is not child._load_enterprise_config.return_value.config


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"mcp": None}, {"mcp": {}}])
async def test_enterprise_mcp_names_allow_missing_section(config):
    child = _child([])
    child._config_base_cache = None
    child._load_enterprise_config.return_value.config = config
    request = SimpleNamespace(channel_id="web")

    assert await child.load_enterprise_mcp_names(request) == set()
    child._load_enterprise_config.assert_awaited_once_with(request, base={})


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
@pytest.mark.parametrize("selected", [set(), {"existing-mcp"}])
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
@pytest.mark.parametrize("picker", [None, [], ["picked-mcp"]])
async def test_enterprise_load_failure_uses_picker(enterprise, selected, error_type, picker):
    child = _child([])
    child._session_selected_mcp.update(selected)
    child._load_enterprise_config.side_effect = error_type("policy store unavailable")
    original_base = child._config_base_cache
    baseline = copy.deepcopy(original_base)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)

    await adapter.reconcile_session_mcp(
        "session-a", picker,
        execution_request=SimpleNamespace(channel_id="web"),
    )

    expected = set(picker or [])
    assert child._session_selected_mcp == expected
    assert child._config_base_cache is original_base
    assert original_base == baseline
    if expected:
        child.register_mcp_by_name.assert_awaited_once_with("picked-mcp")
    else:
        child.register_mcp_by_name.assert_not_awaited()
    if selected:
        child.unregister_mcp_by_name.assert_awaited_once_with("existing-mcp")
    else:
        child.unregister_mcp_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_enterprise_load_failure_returns_none():
    child = _child([])
    child._session_selected_mcp.add("existing-mcp")
    child._load_enterprise_config.side_effect = RuntimeError("policy store unavailable")

    names = await child.load_enterprise_mcp_names(SimpleNamespace(channel_id="web"))
    assert names is None

    assert child._session_selected_mcp == {"existing-mcp"}


@pytest.mark.asyncio
async def test_enterprise_load_retries_and_applies_recovered_policy(enterprise):
    child = _child([])
    child._session_selected_mcp.add("old-mcp")
    child._load_enterprise_config.side_effect = [
        RuntimeError("policy store unavailable"),
        SimpleNamespace(config={"mcp": {"servers": [{"name": "new-mcp"}]}}),
    ]
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)
    request = SimpleNamespace(channel_id="web")

    await adapter.reconcile_session_mcp("session-a", ["picked-mcp"], execution_request=request)
    assert child._session_selected_mcp == {"picked-mcp"}
    child.register_mcp_by_name.assert_awaited_once_with("picked-mcp")
    child.unregister_mcp_by_name.assert_awaited_once_with("old-mcp")
    child.register_mcp_by_name.reset_mock()
    child.unregister_mcp_by_name.reset_mock()

    await adapter.reconcile_session_mcp("session-a", None, execution_request=request)

    assert child._load_enterprise_config.await_count == 2
    assert child._session_selected_mcp == {"new-mcp"}
    child.register_mcp_by_name.assert_awaited_once_with("new-mcp")
    child.unregister_mcp_by_name.assert_awaited_once_with("picked-mcp")


@pytest.mark.asyncio
async def test_enterprise_load_cancellation_propagates(enterprise):
    child = _child([])
    child._session_selected_mcp.add("existing-mcp")
    child._load_enterprise_config.side_effect = asyncio.CancelledError()
    original_base = child._config_base_cache
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)

    with pytest.raises(asyncio.CancelledError):
        await adapter.reconcile_session_mcp(
            "session-a", None, execution_request=SimpleNamespace(channel_id="web"),
        )

    assert child._session_selected_mcp == {"existing-mcp"}
    assert child._config_base_cache is original_base
    child.register_mcp_by_name.assert_not_awaited()
    child.unregister_mcp_by_name.assert_not_awaited()


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
