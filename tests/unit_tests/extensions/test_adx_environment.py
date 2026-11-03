"""Environment-backed routing retains Gateway-owned launch specifications."""
import base64
import json

import httpx
import pytest

from gateway.extensions.adx.config import load_router_config
from gateway.extensions.adx.control_client import AdxControlClient, AdxControlError, AdxCreateTimeout


def config(monkeypatch, tmp_path):
    monkeypatch.setenv('TEST_ADX_KEY', 'platform-secret')
    return load_router_config({'gateway': {
        'agent_client': {'type': 'adx_router', 'agent_namespace': 'test'},
        'adx': {'ingress_url': 'https://adx.example', 'tenant': 'tenant',
                'token_env': 'TEST_ADX_KEY', 'runtime_class': 'runc'},
        'agentos': {'workspace_root': str(tmp_path), 'connect_warmup_enabled': False},
    }})


def envelope(data):
    return {'code': 200, 'message': '', 'data': base64.b64encode(json.dumps(data).encode()).decode()}


@pytest.mark.asyncio
@pytest.mark.parametrize(('name', 'image', 'from_image'), [
    ('alice+jiuwenswarm', 'jiuwenswarm-agent-runtime:latest', True),
    ('alice+custom', 'jiuwenswarm-agent-runtime:latest', False),
    ('alice+jiuwenswarm', 'custom-agent:1', False),
])
async def test_builtin_can_use_deployed_image_process(monkeypatch, name, image, from_image):
    monkeypatch.setenv('TEST_ADX_KEY', 'platform-secret')
    cfg = load_router_config({'gateway': {
        'adx': {'ingress_url': 'https://adx.example', 'tenant': 'tenant',
                'token_env': 'TEST_ADX_KEY', 'builtin_process_from_image': True},
    }})
    exists = False

    def handle(request):
        nonlocal exists
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id': 'default-' + name,
                'tenant_id': 'tenant', 'status': 'running'}]) if exists else httpx.Response(404)
        body = json.loads(request.content)
        assert body['inheritEntrypoint'] is True
        if from_image:
            assert 'process' not in body
        else:
            assert body['process'] == {'commands': [['/bin/app']], 'user': 'agentos'}
        exists = True
        return httpx.Response(200, json=envelope({'environmentId': 'default-' + name}))

    client = AdxControlClient(cfg, transport=httpx.MockTransport(handle))
    try:
        await client.create_sandbox(namespace='default', name=name, workspace='/mnt/alice',
            runtime_spec={'rootfs': {'imageurl': image, 'user': 'agentos'}, 'cmds': [['/bin/app']]})
    finally:
        await client.close()


async def test_environment_create_query_delete_uses_original_spec(monkeypatch, tmp_path):
    cfg = config(monkeypatch, tmp_path)
    calls = []
    exists = False

    def handle(request):
        nonlocal exists
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer platform-secret'
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id': 'test-alice+jiuwenswarm', 'tenant_id': 'tenant',
                'status': 'running'}]) if exists else httpx.Response(404)
        if request.method == 'POST':
            exists = True
            return httpx.Response(200, json=envelope({'environmentId': 'test-alice+jiuwenswarm', 'status': 'running'}))
        return httpx.Response(200, json={'code': 200, 'message': '', 'data': None})

    client = AdxControlClient(cfg, transport=httpx.MockTransport(handle))
    spec = {'sandbox_type': 'supervisor', 'runtime': 'python3.11',
            'rootfs': {'imageurl': 'agent-runtime:1', 'user': '10001:10001', 'ports': ['tcp:18092']},
            'cpu': 2000, 'memory': 4096, 'cmds': [['/bin/app', '--port', '18092']]}
    args = dict(namespace='test', name='alice+jiuwenswarm', workspace='/mnt/users/alice',
                runtime_spec=spec, env_vars={'MODEL_API_BASE': 'http://model/v1'})
    first = await client.create_sandbox(**args)
    second = await client.create_sandbox(**args)
    assert first.sandbox_id == second.sandbox_id == 'test-alice+jiuwenswarm'
    creates = [request for request in calls if request.method == 'POST']
    assert len(creates) == 1
    body = json.loads(creates[0].content)
    assert body['namespace'] == cfg.router.agent_namespace != cfg.tenant
    assert second.metadata['tenant_id'] == cfg.tenant
    assert body['rootfs'] == {'type': 'image', 'imageurl': 'agent-runtime:1', 'runtime': 'runc'}
    assert body['process'] == {'commands': spec['cmds'], 'user': '10001:10001'}
    assert body['cpu'] == 2000 and body['memory'] == 4096
    assert body['env']['MODEL_API_BASE'] == 'http://model/v1'
    assert body['mounts'][0]['host_path'] == '/mnt/users/alice'
    assert body['mounts'][0]['target'] == '/home/agentos'
    assert body['mounts'][0]['options'] == ['rbind', 'rw']
    await client.delete_sandbox(first.sandbox_id)
    assert calls[-1].url.path == '/api/environment/v1/environments/test-alice+jiuwenswarm'
    assert all('/api/agent/' not in request.url.path for request in calls)
    await client.close()


async def test_create_timeout_retries_same_operation_and_never_changes_instance(monkeypatch, tmp_path):
    ids = []
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(404)
        ids.append(request.headers['x-request-id'])
        raise httpx.ReadTimeout('response lost', request=request)
    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    args = dict(namespace='test', name='alice+jiuwenswarm', workspace='/mnt/users/alice',
                runtime_spec={'rootfs': {'imageurl': 'app:1'}, 'cpu': 1000, 'memory': 512})
    for _ in range(2):
        with pytest.raises(AdxControlError):
            await client.create_sandbox(**args)
    assert len(ids) == 2 and ids[0] == ids[1]
    await client.close()


async def test_environment_query_rejects_mismatched_tenant_and_identity(monkeypatch, tmp_path):
    cfg = config(monkeypatch, tmp_path)
    for view in [
        {'id': 'test-alice', 'tenant_id': 'other', 'status': 'running'},
        {'id': 'test-alice', 'tenant_id': cfg.router.agent_namespace, 'status': 'running'},
        {'id': 'test-bob', 'tenant_id': 'tenant', 'status': 'running'},
    ]:
        client = AdxControlClient(cfg, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[view])))
        with pytest.raises(AdxControlError):
            await client.get_agent_info('test-alice')
        await client.close()


async def test_wait_running_timeout_uses_the_existing_lifecycle_error(monkeypatch, tmp_path):
    from dataclasses import replace
    from gateway.extensions.yuanrong_frontend_client import YuanrongAgentTimeoutError

    cfg = replace(config(monkeypatch, tmp_path), connect_timeout_seconds=0.01)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[{
            'id': 'test-alice', 'tenant_id': cfg.tenant, 'status': 'creating',
        }])

    client = AdxControlClient(cfg, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AdxCreateTimeout) as error:
            await client.wait_until_running('test-alice')
        assert isinstance(error.value, YuanrongAgentTimeoutError)
        assert error.value.code == 'SERVICE_UNAVAILABLE'
        assert isinstance(error.value.__cause__, TimeoutError)
        assert calls and all(request.method == 'GET' for request in calls)
    finally:
        await client.close()
