"""Managed runsc policies select business destinations without copying secrets."""
import json

import pytest

from jiuwenswarm.extensions.adx.network import agent_policy, public_policy, tool_policy, workspace_model_origins


def test_model_origins_come_from_retained_config_and_environment(tmp_path):
    config = tmp_path / '.jiuwenswarm/config'
    config.mkdir(parents=True)
    (config / '.env').write_text('MODEL_ENDPOINT=https://model.example/v1\nMODEL_KEY=secret-value\n')
    (config / 'config.yaml').write_text('models:\n  primary:\n    api_base: ${MODEL_ENDPOINT}\n    api_key: ${MODEL_KEY}\n')
    origins = workspace_model_origins(tmp_path)
    assert origins == ['https://model.example/v1']
    wire = public_policy(agent_policy({'ingress_url': 'http://10.88.4.1:8080'}, origins, []))
    assert 'secret-value' not in json.dumps(wire)
    assert wire['traffic']['egressDefaultAction'] == 'allow'
    assert wire['dns']['rules'] == [{'action': 'allow', 'pattern': 'model.example'}]


def test_gateway_traffic_alias_survives_node_changes_without_a_dns_grant():
    policy = agent_policy({'ingress_url': 'http://10.88.4.1:8080',
                           'ingress_policy_peer': 'agentos-ingress.node.invalid'}, [], [])
    rule = policy['traffic']['rules'][0]
    assert rule['domain'] == 'agentos-ingress.node.invalid'
    assert 'cidr' not in rule
    assert rule['port_range'] == {'first': 8080, 'last': 8080}
    assert policy['dns']['rules'] == []


def test_n0_tools_have_no_gateway_or_dns_grant():
    policy = public_policy(tool_policy([], profile='N0'))
    assert policy['traffic']['rules'] == []
    assert policy['dns'] == {'defaultAction': 'deny', 'rules': []}


def test_private_models_require_an_explicit_deployment_origin():
    with pytest.raises(ValueError, match='explicit allowed origin'):
        agent_policy({'ingress_url': 'http://10.88.4.1:8080'}, ['http://192.168.77.10:18000/v1'], [])
    policy = agent_policy({'ingress_url': 'http://10.88.4.1:8080'}, ['http://192.168.77.10:18000/v1'], ['http://192.168.77.10:18000'])
    assert policy['traffic']['rules'][-1]['priority'] == 300


def test_legacy_environment_model_fallback_is_scoped_to_workspace(tmp_path, monkeypatch):
    config = tmp_path / '.jiuwenswarm/config'
    config.mkdir(parents=True)
    (config / 'config.yaml').write_text('sandbox: {}\n')
    (config / '.env').write_text('API_BASE=https://tenant.example/v1\n')
    monkeypatch.setenv('API_BASE', 'https://gateway.example/v1')
    assert workspace_model_origins(tmp_path) == ['https://tenant.example/v1']
    import os
    assert os.environ['API_BASE'] == 'https://gateway.example/v1'


def test_unconfigured_model_grants_no_model_network_access(tmp_path, monkeypatch):
    config = tmp_path / '.jiuwenswarm/config'
    config.mkdir(parents=True)
    (config / 'config.yaml').write_text('models: {}\n')
    (config / '.env').write_text('API_BASE=\n')
    monkeypatch.setenv('API_BASE', 'https://gateway-only.example/v1')
    origins = workspace_model_origins(tmp_path)
    assert origins == []
    policy = agent_policy({'ingress_url': 'http://10.88.4.1:8080'}, origins, [], profile='N0')
    assert len(policy['traffic']['rules']) == 1
    assert policy['traffic']['rules'][0]['cidr'] == '10.88.4.1/32'
    assert policy['dns']['rules'] == []


def test_managed_legacy_denials_are_retained_and_unknown_policy_needs_migration():
    from jiuwenswarm.extensions.adx.network import managed_tool_policy
    blocked = managed_tool_policy({'block_network': True})
    assert blocked['traffic']['egress_default_action'] == 'deny'
    blacklist = managed_tool_policy({'dns_blacklist': ['blocked.example']})
    assert blacklist['dns']['rules'] == [{'action': 'deny', 'pattern': 'blocked.example'}]
    for old in ({'network_mode': 'host'}, {'traffic': {'egress_default_action': 'deny'}}):
        with pytest.raises(ValueError):
            managed_tool_policy(old)


@pytest.mark.asyncio
async def test_model_policy_refresh_serializes_changes_and_does_not_reuse_old_operation(monkeypatch):
    import asyncio
    import httpx
    from test_adx_router import _config
    from jiuwenswarm.extensions.adx.control_client import AdxControlClient
    origins = ['https://one.example/v1']
    monkeypatch.setattr('jiuwenswarm.extensions.adx.network.workspace_model_origins', lambda _: list(origins))
    requests = []
    def handle(request):
        assert request.headers['x-adx-request-id'] == request.headers['x-request-id']
        assert request.headers['x-adx-request-id'].startswith('network-')
        requests.append((request.headers['x-request-id'], json.loads(request.content)))
        return httpx.Response(200, json={'code': 200, 'data': None})
    launch = {'process': {'commands': [['true']]}, 'network': {'ingress_url': 'http://10.88.4.1:8080'}}
    client = AdxControlClient(_config(monkeypatch, builtin_launch=launch), transport=httpx.MockTransport(handle))
    try:
        await asyncio.gather(client.ensure_builtin_network('adx-alice', '/alice'), client.ensure_builtin_network('adx-alice', '/alice'))
        origins[:] = ['https://two.example/v1']
        await client.ensure_builtin_network('adx-alice', '/alice')
        origins[:] = ['https://one.example/v1']
        await client.ensure_builtin_network('adx-alice', '/alice')
        assert len(requests) == 3
        assert len({operation for operation, _ in requests}) == 3
        assert requests[0][1] == requests[2][1]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_policy_replacement_is_reconciled_before_a_different_model(monkeypatch):
    import httpx
    from test_adx_router import _config
    from jiuwenswarm.extensions.adx.control_client import AdxControlClient, AdxControlError
    origins = ['https://one.example/v1']
    monkeypatch.setattr('jiuwenswarm.extensions.adx.network.workspace_model_origins', lambda _: list(origins))
    requests = []
    def handle(request):
        assert request.headers['x-adx-request-id'] == request.headers['x-request-id']
        assert request.headers['x-adx-request-id'].startswith('network-')
        requests.append((request.headers['x-request-id'], json.loads(request.content)))
        if len(requests) == 1:
            raise httpx.ReadTimeout('unknown outcome', request=request)
        return httpx.Response(200, json={'code': 200, 'data': None})
    launch = {'process': {'commands': [['true']]}, 'network': {'ingress_url': 'http://10.88.4.1:8080'}}
    client = AdxControlClient(_config(monkeypatch, builtin_launch=launch), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AdxControlError):
            await client.ensure_builtin_network('adx-alice', '/alice')
        origins[:] = ['https://two.example/v1']
        await client.ensure_builtin_network('adx-alice', '/alice')
        assert len(requests) == 3
        assert requests[0] == requests[1]
        assert requests[2][0] != requests[1][0]
        assert requests[2][1] != requests[1][1]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_policy_refresh_failure_releases_the_business_activity_hold(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_adx_router import _config, _router
    from jiuwenswarm.extensions.adx.control_client import AdxControlError
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
    from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentCreateFailed
    runtime = SimpleNamespace(info=SimpleNamespace(agent_type='jiuwenswarm', user_id='alice', sandbox_id='adx-alice', agent_id='generation-1'), key=('alice', 'jiuwenswarm'))
    monkeypatch.setattr(AgentOSRouterClient, '_resolve_agent', AsyncMock(return_value=runtime))
    launch = {'process': {'commands': [['true']]}, 'network': {'ingress_url': 'http://10.88.4.1:8080'}}
    router, _ = _router(_config(monkeypatch, builtin_launch=launch))
    monkeypatch.setattr(router._control, 'ensure_builtin_network', AsyncMock(side_effect=AdxControlError('policy rejected')))
    release = AsyncMock()
    monkeypatch.setattr(router._agent_manager, 'release', release)
    with pytest.raises(AgentCreateFailed, match='policy rejected'):
        await router._resolve_agent(None, acquire=True)
    release.assert_awaited_once_with(runtime.key, expected_agent_id=runtime.info.agent_id)


@pytest.mark.asyncio
async def test_delete_waits_for_policy_update_and_clears_generation_cache(monkeypatch):
    import asyncio
    import httpx
    from test_adx_router import _config
    from jiuwenswarm.extensions.adx.control_client import AdxControlClient
    monkeypatch.setattr('jiuwenswarm.extensions.adx.network.workspace_model_origins', lambda _: [])
    updating = asyncio.Event()
    finish_update = asyncio.Event()
    requests = []

    async def handle(request):
        requests.append(request.method)
        if request.method == 'PUT' and requests.count('PUT') == 1:
            updating.set()
            await finish_update.wait()
        return httpx.Response(200, json={'code': 200, 'data': None})

    launch = {'process': {'commands': [['true']]}, 'network': {'ingress_url': 'http://10.88.4.1:8080'}}
    client = AdxControlClient(_config(monkeypatch, builtin_launch=launch), transport=httpx.MockTransport(handle))
    update = asyncio.create_task(client.ensure_builtin_network('adx-alice', '/alice'))
    deletion = None
    try:
        await asyncio.wait_for(updating.wait(), 2)
        deletion = asyncio.create_task(client.delete_sandbox('adx-alice'))
        await asyncio.sleep(0)
        assert requests == ['PUT'], 'Deletion must follow the in-flight policy replacement'
        finish_update.set()
        await asyncio.gather(update, deletion)
        await client.ensure_builtin_network('adx-alice', '/alice')
        assert requests == ['PUT', 'DELETE', 'PUT']
    finally:
        finish_update.set()
        await asyncio.gather(update, *([deletion] if deletion else []), return_exceptions=True)
        await client.close()
