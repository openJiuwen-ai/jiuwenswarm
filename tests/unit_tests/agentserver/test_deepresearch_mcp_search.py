"""Native SDK search configuration must cross the isolated runner boundary."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt


SERVER = {"server_name": "search", "server_url": "https://mcp.tavily.com/mcp/",
          "transport_type": "streamable_http", "headers": {"Authorization": "test-credential-7342"},
          "timeout": 30.0, "type": "search"}


@pytest.mark.asyncio
async def test_execution_passes_native_mcp_without_persisting_credentials():
    saved = []
    token = de.bind_deepresearch_execution_context(
        tool_call_id="mcp-test", state=None, user_input=None, model=None,
        save_state=lambda value: saved.append(dict(value)), mcp_servers=[SERVER],
    )
    try:
        with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock(
            return_value=json.dumps({"status": "error", "error_code": "test_stop"})
        )) as stream:
            await de.deepresearch_execute._func(query="研究资料", report_type="professional")
        assert stream.await_args.kwargs["_mcp_servers"] == [SERVER]
        assert saved[-1]["mcp_server_names"] == ["search"]
        assert "test-credential-7342" not in json.dumps(saved)
    finally:
        de.reset_deepresearch_execution_context(token)


def test_nested_mcp_credentials_are_redacted_individually():
    config = {"MCP_SERVERS_JSON": json.dumps([SERVER])}
    assert "test-credential-7342" in dt._config_secret_values(config)


def test_projection_uses_remote_parameters_with_host_filtered_tools():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import project_search_server
    params = {"_mcp_client_type": "streamable-http", "server_path": SERVER["server_url"],
              "auth_headers": SERVER["headers"], "timeout_s": 30}
    assert project_search_server("search", {}, params) == SERVER
    assert project_search_server("search", {"tool_filter": ["search"]}, params) == SERVER
    assert project_search_server("search", {}, {**params, "_mcp_client_type": "stdio"}) is None
    custom = project_search_server("custom", {}, {**params, "server_path": "https://custom.example.com/mcp"})
    assert custom is not None
    assert custom["server_url"] == "https://custom.example.com/mcp"


def test_resume_only_uses_original_source_with_current_credentials():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import source_identity
    current = {**SERVER, "headers": {"Authorization": "rotated"}}
    context = type("Context", (), {"mcp_servers": [current, {**SERVER, "server_name": "new-source"}]})()
    state = {"mcp_source_identities": {"search": source_identity(SERVER)}}
    assert de._current_mcp_servers(context, state) == [current]
    assert de._current_mcp_servers(context, {}) is None
    assert de._current_mcp_servers(context, {"mcp_source_identities": {"revoked": "old"}}) is None


@pytest.mark.asyncio
async def test_adapter_provider_requires_current_request_and_permission():
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    registration = SimpleNamespace(request_id="req-1", session_id="session-1", tool_instances=(
        SimpleNamespace(server_name="search", card=SimpleNamespace(name="search")),))
    adapter = SimpleNamespace(
        _deepresearch_mcp_snapshot=(registration, [SERVER]), _active_office_claw_mcp=registration,
        _current_request_route={"request_id": "req-1", "session_id": "session-1"},
        _resolve_permission_config_for_agent=lambda **kwargs: {"enabled": False},
    )
    read = JiuWenSwarmDeepAdapter._get_deepresearch_mcp_context
    values = await read(adapter)
    assert values == [SERVER]
    values[0]["headers"].clear()
    assert SERVER["headers"]
    adapter._current_request_route["request_id"] = "other-request"
    assert await read(adapter) == []
    adapter._current_request_route["request_id"] = "req-1"
    adapter._resolve_permission_config_for_agent = lambda **kwargs: {"enabled": True}
    assert await read(adapter) == []


@pytest.mark.parametrize('changed', [
    {'server_url': 'https://other.example/mcp'},
    {'server_url': SERVER['server_url'] + '?scope=other'},
    {'transport_type': 'sse'},
])
def test_resume_rejects_same_name_changed_source(changed):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import source_identity
    state = {'mcp_source_identities': {'search': source_identity(SERVER)}}
    context = type('Context', (), {'mcp_servers': [{**SERVER, **changed}]})()
    assert de._current_mcp_servers(context, state) is None


def test_projection_rejects_credentials_not_redacted_by_native_sdk():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import project_search_server
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'],
              'auth_headers': {'X-Session-ID': 'secret'}}
    assert project_search_server('search', {}, params) is None
    params.update(auth_headers={}, server_path=SERVER['server_url'] + '?sid=secret')
    assert project_search_server('search', {}, params) is None


@pytest.mark.parametrize('installed', [[], [('search', 'search')], [('other', 'search'), ('search', 'lookup')]])
def test_partial_admission_cannot_be_delegated(installed):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import fully_admitted_servers
    assert fully_admitted_servers([SERVER], {'search': ['search', 'lookup']}, installed) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['complete', 'duplicate', 'builtin', 'install_failure'])
async def test_registration_delegates_only_complete_server(monkeypatch, condition):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
    from test_request_scoped_office_claw_mcp import (
        _ResourceManager, _bare_session_adapter, _request_with_connectors,
    )
    manager = _ResourceManager()
    if condition == 'install_failure':
        original = manager.add_tool
        manager.add_tool = lambda tool, **kw: False if tool.card.name == 'lookup' else original(tool, **kw)
    monkeypatch.setattr(adapter_module.Runner, 'resource_mgr', manager)
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'],
              'auth_headers': SERVER['headers'], 'timeout_s': 30}

    async def discover(name, config):
        names = ['search'] if name == 'first' else ['search', 'lookup']
        return ([{'name': n, 'input_params': {}} for n in names], params)

    monkeypatch.setattr(adapter_module, 'list_request_mcp_server_tools', discover)
    adapter = _bare_session_adapter()
    if condition == 'builtin':
        from openjiuwen.core.foundation.tool.base import ToolCard
        adapter._instance.ability_manager.cards['lookup'] = ToolCard(
            id='builtin-lookup', name='lookup', input_params={})
    connectors = {'search': {}}
    if condition == 'duplicate':
        connectors = {'first': {}, **connectors}
    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request_with_connectors(connectors=connectors))
    try:
        snapshot = adapter._deepresearch_mcp_snapshot
        assert snapshot[0] is registration
        names = [s['server_name'] for s in snapshot[1]]
        assert names == (['search'] if condition == 'complete' else ['first'] if condition == 'duplicate' else [])
    finally:
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)


def test_url_credential_rotation_preserves_source_but_scope_changes_do_not():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import project_search_server, source_identity
    original = {**SERVER, 'server_url': SERVER['server_url'] + '?tavilyApiKey=old'}
    rotated = {**SERVER, 'server_url': SERVER['server_url'] + '?tavilyApiKey=new'}
    assert source_identity(original) == source_identity(rotated)
    assert source_identity({**SERVER, 'server_url': SERVER['server_url'] + '?auth_scope=a'}) != source_identity(
        {**SERVER, 'server_url': SERVER['server_url'] + '?auth_scope=b'})
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'] + '?auth_scope=a'}
    assert project_search_server('search', {}, params) is None


@pytest.mark.parametrize("session,disabled", [("other", set()), ("original", {"search"})])
@pytest.mark.asyncio
async def test_provider_rejects_cross_session_and_disabled_tool(session, disabled):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    tool = SimpleNamespace(server_name='search', card=SimpleNamespace(name='search'))
    registration = SimpleNamespace(request_id='req', session_id='original', tool_instances=(tool,))
    adapter = SimpleNamespace(
        _deepresearch_mcp_snapshot=(registration, [SERVER]), _active_office_claw_mcp=registration,
        _current_request_route={'request_id': 'req', 'session_id': session},
        _disabled_tools_rail=SimpleNamespace(_disabled_tools=disabled),
        _resolve_permission_config_for_agent=lambda **kw: {'enabled': False},
        _config_cache={},
    )
    read = JiuWenSwarmDeepAdapter._get_deepresearch_mcp_context
    assert await read(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,node', [('wait_feedback', 'feedback_handler'), ('wait_outline', 'outline_interaction')])
async def test_both_resume_branches_forward_only_original_source(phase, node):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import source_identity
    current = {**SERVER, 'headers': {'Authorization': 'rotated'}}
    state = {'schema_version': 1, 'phase': phase, 'query': '研究资料', 'conversation_id': 'mcp-resume',
             'questions': ['关注哪个方面？'], 'outline_presented': True, 'revision': 1,
             'requested_report_type': 'professional',
             'mcp_source_identities': {'search': source_identity(SERVER)}}
    token = de.bind_deepresearch_execution_context(
        tool_call_id='mcp-resume', state=state, model=None, save_state=lambda state: None,
        user_input={'status': 'answered', 'answers': [{'selected_options': ['确认大纲，继续研究']}]},
        mcp_servers=[current, {**SERVER, 'server_name': 'unrelated'}])
    try:
        with patch.object(de, '_call_deepresearch_stream_impl', new=AsyncMock(return_value=json.dumps({
            'status': 'completed', 'conversation_id': 'mcp-resume', 'report_delivered': True}))) as stream:
            await de.deepresearch_execute._func(query='研究资料')
        assert stream.await_count == 1
        assert stream.await_args.kwargs['node'] == node
        assert stream.await_args.kwargs['_mcp_servers'] == [current]
    finally:
        de.reset_deepresearch_execution_context(token)


@pytest.mark.parametrize('level,rules,expected', [('allow', [], True), ('ask', [], False), ('deny', [], False),
                                                ('allow', [{'tools': ['search'], 'pattern': 'secret',
                                                            'action': 'deny'}], True)])
@pytest.mark.asyncio
async def test_provider_exposes_only_host_allowed_tools(level, rules, expected):
    from types import SimpleNamespace
    from openjiuwen.harness.security.core import PermissionEngine
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    tool = SimpleNamespace(server_name='search', card=SimpleNamespace(name='search'))
    registration = SimpleNamespace(request_id='req', session_id='session', tool_instances=(tool,))
    policy = {'enabled': True, 'tools': {'search': level}, 'rules': rules, 'file_guard': {'enabled': False}}
    adapter = SimpleNamespace(
        _deepresearch_mcp_snapshot=(registration, [SERVER]), _active_office_claw_mcp=registration,
        _current_request_route={'request_id': 'req', 'session_id': 'session'},
        _resolve_permission_config_for_agent=lambda **kw: policy,
        _permission_rail=SimpleNamespace(_engine=PermissionEngine(config=policy)),
    )
    assert bool(await JiuWenSwarmDeepAdapter._get_deepresearch_mcp_context(adapter)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_filter,expected', [(None, True), (['search'], True)])
async def test_registry_connection_respects_full_tool_policy(monkeypatch, tool_filter, expected):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
    from test_request_scoped_office_claw_mcp import _ResourceManager, _bare_session_adapter, _request_with_connectors
    params = {'_mcp_client_type': 'streamable-http', 'server_path': SERVER['server_url'],
              'auth_headers': SERVER['headers'], 'timeout_s': 30}
    registry = SimpleNamespace(
        snapshot_for_chat=AsyncMock(return_value=[('search', [{'name': 'search', 'input_params': {}}], params)]),
        get_server=AsyncMock(return_value={'enabled': True, 'config': {'tool_filter': tool_filter}}),
    )
    monkeypatch.setattr(adapter_module.Runner, 'resource_mgr', _ResourceManager())
    monkeypatch.setattr(adapter_module, 'get_mcp_server_registry', lambda: registry)
    adapter = _bare_session_adapter()
    request = _request_with_connectors()
    request.params['mcp_server_list'] = ['search']
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    try:
        assert bool(adapter._deepresearch_mcp_snapshot[1]) == expected
    finally:
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:63890/mcp',
    'https://internal.example.net/custom-mcp',
    'http://intranet.example.net/mcp',
])
def test_custom_mcp_uses_sdk_default_search_purpose(url):
    from openjiuwen_deepsearch.config.config import McpServerRuntimeConfig
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import project_search_server
    params = {'_mcp_client_type': 'streamable-http', 'server_path': url}
    result = project_search_server('custom', {}, params)
    assert result is not None
    assert result['server_url'] == url
    assert result['type'] == McpServerRuntimeConfig.model_fields['type'].default == 'search'


@pytest.mark.parametrize('registered', [False, True])
@pytest.mark.asyncio
async def test_custom_mcp_keeps_host_tool_selection_without_keyword_classification(monkeypatch, registered):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
    from test_request_scoped_office_claw_mcp import (
        _ResourceManager, _bare_session_adapter, _request_with_connectors,
    )
    params = {'_mcp_client_type': 'streamable-http', 'server_path': 'http://127.0.0.1:63890/mcp'}
    definitions = [{'name': name, 'input_params': {}} for name in ['search_project', 'vendor_data']]
    registry = SimpleNamespace(
        snapshot_for_chat=AsyncMock(return_value=[('custom', definitions, params)]),
        get_server=AsyncMock(return_value={'enabled': True, 'config': {}}),
        snapshot_for_deepresearch=AsyncMock(return_value=({}, definitions, params)),
    )
    monkeypatch.setattr(adapter_module.Runner, 'resource_mgr', _ResourceManager())
    monkeypatch.setattr(adapter_module, 'get_mcp_server_registry', lambda: registry)
    monkeypatch.setattr(adapter_module, 'list_request_mcp_server_tools',
                        AsyncMock(return_value=(definitions, params)))
    adapter = _bare_session_adapter()
    adapter._current_request_route = {'request_id': 'req-123', 'session_id': 'session-456'}
    adapter._resolve_permission_config_for_agent = lambda **kwargs: {'enabled': False}
    request = _request_with_connectors(connectors={} if registered else {'custom': {}})
    if registered:
        request.params['mcp_server_list'] = ['custom']
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    try:
        grants = await adapter._get_deepresearch_mcp_context()
        assert len(grants) == 1
        assert set(grants[0].tools) == {'search_project', 'vendor_data'}
        adapter._disabled_tools_rail = SimpleNamespace(_disabled_tools={'vendor_data'})
        assert not await grants[0].allowed('vendor_data', {})
        assert await grants[0].allowed('search_project', {})
    finally:
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)
