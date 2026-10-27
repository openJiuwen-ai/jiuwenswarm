"""Regressions for the native MCP integration's independent code review."""
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
from test_deepresearch_mcp_search import SERVER


@pytest.mark.asyncio
@pytest.mark.parametrize("originally_authorized", [True, False])
async def test_stopped_resume_only_restores_original_mcp_sources(originally_authorized):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import source_identity

    state = {"schema_version": 1, "phase": "new", "revision": 1,
             "query": "研究主题", "conversation_id": "stopped-mcp"}
    if originally_authorized:
        state["mcp_source_identities"] = {SERVER["server_name"]: source_identity(SERVER)}
    token = de.bind_deepresearch_execution_context(
        tool_call_id="stopped-mcp", state=state, user_input=None, model=None,
        save_state=lambda value: None, mcp_servers=[SERVER])
    try:
        with patch.object(de, "_probe_resumable_checkpoint", return_value={"stopped": True, "node": "info_collector"}), \
                patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(return_value={
                    "status": "completed", "report_delivered": True})) as stream:
            result = await de.deepresearch_execute._func(query="研究主题")
    finally:
        de.reset_deepresearch_execution_context(token)
    assert result["kind"] == "completed"
    assert stream.await_args.kwargs["node"] == "stopped"
    assert stream.await_args.kwargs.get("_mcp_servers") == ([SERVER] if originally_authorized else None)


def adapter_for(policy=None, disabled=(), *, registry=False):
    tool = SimpleNamespace(server_name='search', card=SimpleNamespace(name='search', description='Search',
                                                                     input_params={'type': 'object'}),
                           invoke=AsyncMock(return_value={'result': '{}'}))
    registration = SimpleNamespace(request_id='req', session_id='session', tool_instances=(tool,), tool_ids=('search-tool',))
    return SimpleNamespace(
        _deepresearch_mcp_snapshot=(registration, [SERVER], frozenset({'search'}) if registry else frozenset()),
        _active_office_claw_mcp=registration,
        _current_request_route={'request_id': 'req', 'session_id': 'session'},
        _resolve_permission_config_for_agent=lambda **kw: policy or {'enabled': False},
        _permission_rail=SimpleNamespace(_engine=SimpleNamespace(
            check_tool_permission_directly=lambda *args: ('allow', None))),
        _disabled_tools_rail=SimpleNamespace(_disabled_tools=set(disabled)),
    )


async def read(adapter):
    result = adapter_module.JiuWenSwarmDeepAdapter._get_deepresearch_mcp_context(adapter)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['disabled', 'deleted', 'endpoint_changed'])
async def test_registry_revocation_and_source_changes_are_rechecked(monkeypatch, change):
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'],
              'auth_headers': SERVER['headers'], 'timeout_s': 30}
    if change == 'endpoint_changed':
        params['server_path'] = 'https://agent.qcc.com/mcp/company/stream'
    from jiuwenswarm.common.mcp_server_registry import DisabledMcpServerError, UnknownMcpServerError
    registry = SimpleNamespace(snapshot_for_deepresearch=AsyncMock(
        return_value=({}, [{'name': 'search'}], params),
        side_effect=DisabledMcpServerError('search') if change == 'disabled' else
        UnknownMcpServerError('search') if change == 'deleted' else None))
    monkeypatch.setattr(adapter_module, 'get_mcp_server_registry', lambda: registry)
    assert await read(adapter_for(registry=True)) == []



@pytest.mark.asyncio
async def test_registry_credential_rotation_uses_current_parameters(monkeypatch):
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'],
              'auth_headers': {'Authorization': 'rotated'}, 'timeout_s': 30}
    registry = SimpleNamespace(snapshot_for_deepresearch=AsyncMock(
        return_value=({}, [{'name': 'search'}], params)))
    monkeypatch.setattr(adapter_module, 'get_mcp_server_registry', lambda: registry)
    assert await read(adapter_for(registry=True)) == [{**SERVER, 'headers': {'Authorization': 'rotated'}}]



@pytest.mark.asyncio
@pytest.mark.parametrize('policy,disabled', [
    ({'enabled': True, 'tools': {'search': 'allow'}, 'file_guard': {'enabled': False}}, ()),
    ({'enabled': False}, ('new_ungranted_tool',)),
])
async def test_direct_sdk_cannot_receive_connections_with_per_tool_restrictions(policy, disabled):
    # SDK gets the private endpoint's frozen list, never the remote tools/list.
    grants = await read(adapter_for(policy, disabled))
    assert len(grants) == 1
    assert set(grants[0].tools) == {'search'}
    with pytest.raises(PermissionError):
        await grants[0].call('new_ungranted_tool', {})



@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['feedback_handler', 'outline_interaction'])
async def test_claimed_checkpoint_does_not_reauthorize_current_sources(node):
    saved = []
    probe = {'node': node, 'questions': ['研究哪个方向？'],
             'outline_json': {'sections': [{'title': '研究目标', 'subsections': []}]}}
    token = de.bind_deepresearch_execution_context(
        tool_call_id='claim-mcp', state=None, user_input=None, model=None,
        save_state=lambda state: saved.append(dict(state)), mcp_servers=[SERVER])
    try:
        with patch.object(de, '_probe_resumable_checkpoint', return_value=probe), patch.object(
            de, '_generate_options', new=AsyncMock(return_value=[[]])):
            result = await de.deepresearch_execute._func(query='研究资料', conversation_id='old-checkpoint')
        assert result['kind'] == 'interaction'
        assert saved[-1]['conversation_id'] == 'old-checkpoint'
        assert not saved[-1].get('mcp_source_identities')
        assert de._current_mcp_servers(SimpleNamespace(mcp_servers=[SERVER]), saved[-1]) is None
    finally:
        de.reset_deepresearch_execution_context(token)


@pytest.mark.asyncio
async def test_sdk_window_rechecks_authorization_after_material_preparation():
    context = SimpleNamespace(tool_call_id='window', mcp_context_provider=AsyncMock(return_value=[]))
    with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock(
        return_value=json.dumps({'status': 'error'}))) as stream:
        await de._call_sdk(context, action='start', _mcp_servers=[SERVER])
    assert '_mcp_servers' not in stream.await_args.kwargs


@pytest.mark.asyncio
async def test_gateway_uses_latest_permission_without_mutating_host_engine():
    policy = {'enabled': True, 'tools': {'search': 'allow'}, 'file_guard': {'enabled': False}}
    adapter = adapter_for(policy)
    grants = await read(adapter)
    assert len(grants) == 1
    policy['tools']['search'] = 'deny'
    assert not await grants[0].allowed('search', {})


@pytest.mark.asyncio
async def test_owner_scope_cannot_be_bypassed_by_gateway():
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import PermissionContext, TOOL_PERMISSION_CONTEXT
    policy = {'enabled': False, 'owner_scopes': {'web': {'owner': {'tools': {'search': 'deny'}}}}}
    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(channel_id='web', principal_user_id='owner'))
    try:
        assert await read(adapter_for(policy)) == []
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_registry_delegation_snapshot_is_isolated_and_checks_live_status(monkeypatch):
    from test_mcp_server_registry import _ok_discover, _remote_cfg
    from jiuwenswarm.common.mcp_server_registry import (
        McpServerRegistry, DisabledMcpServerError, UnknownMcpServerError,
    )
    monkeypatch.setattr('jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools', _ok_discover)
    registry = McpServerRegistry()
    assert (await registry.add_servers([_remote_cfg('search')]))[0]['ok']
    config, tools, params = await registry.snapshot_for_deepresearch('search')
    config.clear()
    tools.clear()
    params.clear()
    latest = await registry.snapshot_for_deepresearch('search')
    assert latest[0] and latest[1] and latest[2]
    registry._registry['search'].enabled = False
    with pytest.raises(DisabledMcpServerError):
        await registry.snapshot_for_deepresearch('search')
    await registry.remove_servers(['search'])
    with pytest.raises(UnknownMcpServerError):
        await registry.snapshot_for_deepresearch('search')
