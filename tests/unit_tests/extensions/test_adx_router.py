# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway business routing over public ADX Environment lifecycle APIs."""
import asyncio
import base64
import json
import tempfile
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.extensions.adx.config import load_router_config
from jiuwenswarm.extensions.adx.control_client import AdxControlClient
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import InstanceRecord
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    AGENT_GENERATION_ENV, YuanrongAgentNotFoundError, YuanrongAgentGenerationMismatchError,
)


def _config(monkeypatch, *, sandbox=None, **overrides):
    monkeypatch.setenv('TEST_ADX_KEY', 'platform-secret')
    root = Path(tempfile.mkdtemp(prefix='adx-env-gateway-'))
    for user in ('alice', 'bob'):
        (root / user).mkdir()
    settings = {'ingress_url': 'https://adx.example', 'tenant': 'appliance', 'token_env': 'TEST_ADX_KEY',
                'connect_timeout_seconds': 2}
    settings.update(overrides)
    return load_router_config({'gateway': {
        'agent_client': {'type': 'adx_router', 'agent_namespace': 'test'},
        'adx': settings, 'agentos': {'workspace_root': str(root)},
    }, 'sandbox': sandbox})


def test_explicit_http_origin_controls_business_http_and_websocket_schemes(monkeypatch):
    config = _config(monkeypatch, ingress_url='http://127.0.0.1:8080')
    router = AdxRouterClient(config)
    assert urlsplit(router._agent_ws_url('test-alice+jiuwenswarm', 18092)).scheme == 'ws'
    assert urlsplit(router._agent_http_url('test-alice+jiuwenswarm', 18092)).scheme == 'http'
    secure = AdxRouterClient(_config(monkeypatch))
    assert urlsplit(secure._agent_ws_url('test-alice+jiuwenswarm', 18092)).scheme == 'wss'


class EnvironmentAPI:
    def __init__(self, tenant='appliance'):
        self.tenant = tenant
        self.environments = {}
        self.creates = []
        self.requests = []
        self.delete_error = False
        self.delete_committed = False

    def handle(self, request):
        self.requests.append(request)
        assert request.headers['authorization'] == 'Bearer platform-secret'
        if request.method == 'GET' and request.url.path == '/api/instances':
            instance = request.url.params['instance_id']
            return httpx.Response(200, json=[self.environments[instance]]) if instance in self.environments else httpx.Response(404)
        if request.method == 'POST' and request.url.path == '/api/environment/v1/environments':
            body = json.loads(request.content)
            self.creates.append(body)
            instance = body['namespace'] + '-' + body['name']
            self.environments[instance] = {'id': instance, 'tenant_id': self.tenant, 'status': 'running',
                                           'env_vars': dict(body.get('env') or {})}
            result = {'code': 200, 'data': base64.b64encode(json.dumps({'environmentId': instance}).encode()).decode()}
            return httpx.Response(200, json=result)
        if request.method == 'DELETE':
            instance = request.url.path.rsplit('/', 1)[1]
            if self.delete_error:
                if self.delete_committed:
                    self.environments.pop(instance, None)
                raise httpx.ReadTimeout('unknown delete', request=request)
            self.environments.pop(instance, None)
            return httpx.Response(200, json={'success': True})
        raise AssertionError(f'unexpected ADX request: {request.method} {request.url}')


class FakeClient:
    def __init__(self):
        self.server_ready = False
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.calls = []
        self.push = None
        self.closed = None

    async def _disconnect(self):
        self.server_ready = False

    def set_server_push_handler(self, handler):
        self.push = handler

    def set_disconnect_handler(self, handler):
        self.closed = handler

    async def connect(self, uri, extra_headers=None):
        await asyncio.sleep(0)
        self.uri = uri
        self.headers = extra_headers
        self.server_ready = True

    async def send_request(self, envelope, *, timeout=None):
        self.calls.append(envelope)
        return AgentResponse(envelope.request_id, envelope.channel, payload={'user': envelope.user_id})

def _router(config, *, auth_client=None):
    clients = []
    api = EnvironmentAPI(config.tenant)
    control = AdxControlClient(config, transport=httpx.MockTransport(api.handle))
    def create():
        client = FakeClient()
        clients.append(client)
        return client
    router = AdxRouterClient(
        config, ws_client_factory=create, control_client=control, auth_client=auth_client,
    )
    router.set_or_update_server_config(config={})
    router.test_api = api
    return router, clients


@pytest.mark.asyncio
async def test_workspace_failure_blocks_only_its_user_and_is_retryable(monkeypatch):
    from jiuwenswarm.extensions.adx.workspace import MIGRATION_ERROR_FILE
    config = _config(monkeypatch, workspace_config_template='/etc/agentos/prepared.yaml')
    folder = Path(config.router.workspace_root) / 'alice/.jiuwenswarm/config'
    folder.mkdir(parents=True)
    (folder / 'config.yaml').write_text('sandbox: {type: adx-environment}\n')
    marker = folder / MIGRATION_ERROR_FILE
    marker.write_text(json.dumps({'reason': 'sandbox.files requires explicit migration to ADX policy/mounts'}))
    router, _ = _router(config)
    await router.connect('')
    try:
        failed, healthy = await asyncio.gather(router.send_request(_request('alice')),
                                             router.send_request(_request('bob')))
        assert not failed.ok
        assert 'WORKSPACE_CONFIG_INVALID' in failed.payload['error']
        assert 'sandbox.files' in failed.payload['error']
        assert healthy.ok
        assert len(router.test_api.creates) == 1
        assert 'bob' in router.test_api.creates[0]['name']
        marker.unlink()
        assert (await router.send_request(_request('alice', rid='after-migration'))).ok
        assert len(router.test_api.creates) == 2
    finally:
        await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime,status', [
    ('runsc', 'running'), ('runsc', 'paused'),
    ('supervisor', 'running'), ('conch', 'running'),
])
async def test_startup_cleanup_retires_untracked_environments_for_all_runtimes(
    monkeypatch, runtime, status,
):
    router, _ = _router(_config(monkeypatch, runtime_class=runtime))
    instance = 'test-alice+jiuwenswarm'
    router.test_api.environments[instance] = {'id': instance, 'status': status}
    registry = SimpleNamespace(
        enabled=True,
        list_instances=AsyncMock(return_value=[InstanceRecord(
            service_id='existing-service', instance_id=instance, user='alice', framework='jiuwenswarm',
        )]),
        unregister_instance=AsyncMock(return_value={'deleted': True}),
        close=AsyncMock(),
    )
    router._registry = registry
    try:
        await router.connect('')
        if router._stale_cleanup_task is not None:
            await asyncio.wait_for(router._stale_cleanup_task, timeout=2)
        assert instance not in router.test_api.environments
        registry.unregister_instance.assert_awaited_once()
        registry.list_instances.assert_awaited()
    finally:
        await router.shutdown()


def _request(user='alice', rid='same-id', **params):
    return E2AEnvelope(request_id=rid, user_id=user, channel='web', method='chat.send', params=params)


@pytest.mark.asyncio
async def test_native_generation_contract_for_replacement_and_absence(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    instance = 'test-gateway-new-generation'
    try:
        with pytest.raises(YuanrongAgentNotFoundError):
            await router._control.get_agent_info(instance)
        router.test_api.environments[instance] = {
            'id': instance, 'tenant_id': 'appliance', 'status': 'running',
            'env_vars': {AGENT_GENERATION_ENV: 'new-generation'},
        }
        deadline = asyncio.get_running_loop().time() + 1
        result = await router._control.wait_until_running(
            instance, expected_generation='new-generation', deadline=deadline)
        assert result['id'] == instance
        with pytest.raises(YuanrongAgentGenerationMismatchError):
            await router._control.wait_until_running(
                instance, expected_generation='old-generation', deadline=deadline)
    finally:
        await router.shutdown()


def test_generated_gateway_name_keeps_builtin_runsc_launch(monkeypatch):
    launch = {'process': {'commands': [['/opt/python/bin/python', '-m', 'agentserver']]},
              'mounts': [{'type': 'bind', 'host_path': '/opt/python', 'target': '/opt/python', 'options': ['ro']}],
              'env': {'JIUWENSWARM_ADX_NODE_CONTEXT_FILE': '/run/agentos/node.json'}}
    control = AdxControlClient(_config(monkeypatch, runtime_class='runsc',
        builtin_image='ubuntu:24.04', builtin_launch=launch))
    body = control._creation_body('test', 'gateway-new-generation', '/workspace',
        {'rootfs': {'imageurl': 'jiuwenswarm-agent-runtime:latest'}},
        {AGENT_GENERATION_ENV: 'new-generation'}, None, builtin=True)
    assert body['rootfs']['imageurl'] == 'ubuntu:24.04'
    assert body['process'] == launch['process']
    assert body['env']['JIUWENSWARM_ADX_API_TOKEN'] == 'platform-secret'
    assert launch['mounts'][0] in body['mounts']


@pytest.mark.parametrize('name', ['alice+jiuwenswarm', 'gateway-new-generation'])
def test_builtin_without_image_uses_node_rootfs_and_keeps_launch(monkeypatch, name):
    launch = {'process': {'commands': [['/opt/python/bin/python', '-m', 'agentserver']],
                          'cwd': '/home/agentos'},
              'mounts': [{'type': 'bind', 'host_path': '/opt/python',
                          'target': '/opt/python', 'options': ['ro']}],
              'env': {'HOME': '/home/agentos'}}
    control = AdxControlClient(_config(monkeypatch, runtime_class='runsc', builtin_launch=launch))
    body = control._creation_body('test', name, '/workspace',
        {'rootfs': {'imageurl': 'jiuwenswarm-agent-runtime:latest'}}, None, None, builtin=True)
    assert 'rootfs' not in body
    assert not body.get('inheritEntrypoint', False)
    assert body['process'] == launch['process']
    assert launch['mounts'][0] in body['mounts']
    assert body['env']['JIUWENSWARM_ADX_API_TOKEN'] == 'platform-secret'


@pytest.mark.asyncio
@pytest.mark.parametrize('instance', [
    'test-alice+jiuwenswarm', 'test-gateway-existing-generation',
    'test-alice+jiuwenswarm-0123456789ab',
])
@pytest.mark.parametrize('status', ['running', 'paused'])
async def test_first_request_after_restart_reuses_native_registered_environment(monkeypatch, instance, status):
    router, clients = _router(_config(monkeypatch, runtime_class='runsc'))
    router.test_api.environments[instance] = {'id': instance, 'tenant_id': 'appliance', 'status': status}
    router._registry.list_instances = AsyncMock(return_value=[InstanceRecord(
        service_id='persistent', instance_id=instance, user='alice', framework='jiuwenswarm')])
    await router.connect('')
    try:
        assert (await router.send_request(_request())).ok
        assert router.test_api.creates == []
        assert unquote(parse_qs(urlsplit(clients[0].uri).query)['target'][0]) == 'urn:adx:instance:' + instance
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_connect_warmup_survives_a_timed_out_foreground_waiter(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    started = asyncio.Event()
    finish = asyncio.Event()
    original_create = router._create_agent

    async def delayed_create(info):
        started.set()
        await finish.wait()
        return await original_create(info)

    router._create_agent = delayed_create
    await router.connect('')
    try:
        router._schedule_connect_warmup('alice')
        warmup = router._warmup_tasks.get('alice')
        assert warmup is not None
        await asyncio.wait_for(started.wait(), timeout=3)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(router.send_request(_request()), timeout=0.01)
        assert not warmup.done()
        finish.set()
        await asyncio.wait_for(warmup, timeout=3)
        response = await router.send_request(_request(rid='after-timeout'))
        assert response.ok
        assert len(router.test_api.creates) == len(clients) == 1
    finally:
        finish.set()
        await router.shutdown()


@pytest.mark.asyncio
async def test_connect_warmup_can_be_disabled_by_router_settings(monkeypatch):
    config = _config(monkeypatch)
    config = replace(config, router=replace(config.router, connect_warmup_enabled=False))
    router, clients = _router(config)
    await router.connect('')
    try:
        router._schedule_connect_warmup('alice')
        assert not router._warmup_tasks
        assert not router.test_api.creates
        assert not clients
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_users_and_first_creates_are_isolated_and_coalesced(monkeypatch):
    router, clients = _router(_config(monkeypatch, builtin_image='registry.example/agentserver:1'))
    await router.connect('')
    try:
        results = await asyncio.gather(*(router.send_request(_request(user, str(index)))
            for index, user in enumerate(['alice', 'alice', 'bob', 'alice'])))
        assert len(clients) == len(router.test_api.creates) == 2
        assert [result.payload['user'] for result in results] == ['alice', 'alice', 'bob', 'alice']
        for client in clients:
            target = parse_qs(urlsplit(client.uri).query)['target'][0]
            assert target.startswith('urn:adx:instance:test-')
            assert client.headers['Authorization'] == 'Bearer platform-secret'
            assert 'X-ADX-Parameters' not in client.headers
        assert all(body['rootfs']['imageurl'] == 'registry.example/agentserver:1' for body in router.test_api.creates)
        for body in router.test_api.creates:
            user = Path(body['mounts'][0]['host_path']).name
            generation = body['env'][AGENT_GENERATION_ENV]
            assert len(generation) == 32
            assert body['name'] == f'{user}+jiuwenswarm-{generation[:12]}'
        mounts = [body['mounts'][0]['host_path'] for body in router.test_api.creates]
        assert len(set(mounts)) == 2 and all(Path(path).is_dir() for path in mounts)
        assert all(body['process']['commands'][0][-1].startswith('exec jiuwenswarm-agentserver')
                   for body in router.test_api.creates)
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_builtin_launch_uses_configured_agent_and_tool_resources(monkeypatch):
    monkeypatch.setenv('AGENTOS_BUILTIN_AGENT_CPU', '750')
    monkeypatch.setenv('AGENTOS_BUILTIN_AGENT_MEMORY', '1024')
    monkeypatch.setenv('TOOL_SANDBOX_CPU', '900')
    monkeypatch.delenv('TOOL_SANDBOX_MEMORY', raising=False)
    monkeypatch.delenv('TOOL_SANDBOX_IDLE_TIMEOUT', raising=False)
    router, _ = _router(_config(monkeypatch, sandbox={
        'type': 'adx-environment', 'cpu': 1000, 'memory': 1536, 'idle_timeout': 60,
    }))
    await router.connect('')
    try:
        await router.send_request(_request())
        body, = router.test_api.creates
        assert (body['cpu'], body['memory']) == (750, 1024)
        assert body['env']['TOOL_SANDBOX_CPU'] == '900'
        assert body['env']['TOOL_SANDBOX_MEMORY'] == '1536'
        assert body['env']['TOOL_SANDBOX_IDLE_TIMEOUT'] == '60'
    finally:
        await router.shutdown()

@pytest.mark.parametrize('remote_deleted', [False, True])
@pytest.mark.asyncio
async def test_unknown_delete_retains_operation_until_lookup_confirms_result(monkeypatch, remote_deleted):
    router, clients = _router(_config(monkeypatch))
    router._probe_settings = replace(router._probe_settings,
        wait_running_timeout_seconds=0.2, wait_running_interval_seconds=0.01)
    await router.connect('')
    try:
        await router.send_request(_request())
        await asyncio.gather(*tuple(router._background_tasks))
        router._registry.unregister_agent = AsyncMock()
        first_url = clients[0].uri
        router.test_api.delete_error = True
        router.test_api.delete_committed = remote_deleted
        assert await router.delete_agent('alice', 'jiuwenswarm')
        instance = next(iter(router._control._deletes))
        delete_operation = router._control._deletes[instance]
        clients[0].disconnect.assert_awaited_once()
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is None
        router._registry.unregister_agent.assert_awaited_once()
        # An unknown DELETE must never hand its old target back to a caller.
        response = await router.send_request(_request(rid='next'))
        assert response.ok is remote_deleted
        assert len(router.test_api.creates) == (2 if remote_deleted else 1)
        if not remote_deleted:
            assert 'AGENT_REBUILD_PENDING' in response.payload['error']
            assert router._control._deletes[instance] == delete_operation
        router.test_api.delete_error = False
        if not remote_deleted:
            for lifecycle in router._sandbox_lifecycles.values():
                lifecycle.delete_retry_at = 0
            assert (await router.send_request(_request(rid='delete-confirmed'))).ok
            retries = [request for request in router.test_api.requests if request.method == 'DELETE'
                       and request.url.path.endswith('/' + instance)]
            assert all(request.headers['x-request-id'] == delete_operation for request in retries)
        assert len(router.test_api.creates) == 2
        assert clients[-1].uri != first_url
        assert router.test_api.creates[0]['mounts'] == router.test_api.creates[1]['mounts']
        assert await router.delete_agent('alice', 'jiuwenswarm')
        assert (await router.send_request(_request(rid='after-delete'))).ok
        assert len(router.test_api.creates) == 3
        create_ids = [request.headers['x-request-id'] for request in router.test_api.requests
                      if request.method == 'POST']
        assert len(set(create_ids)) == 3
    finally:
        await router.shutdown()


@pytest.mark.parametrize('status', ['failed', 'deleted', 'deleting', 'missing'])
@pytest.mark.asyncio
async def test_terminal_environment_is_cleaned_and_next_request_recreates(monkeypatch, status):
    router, clients = _router(_config(monkeypatch))
    await router.connect('')
    try:
        assert (await router.send_request(_request())).ok
        await asyncio.gather(*tuple(router._background_tasks))
        instance = next(iter(router.test_api.environments))
        await router._close_ws_client(instance)
        if status == 'missing':
            router.test_api.environments.pop(instance)
        else:
            router.test_api.environments[instance]['status'] = status
        router._registry.unregister_agent = AsyncMock()
        assert not (await router.send_request(_request(rid='failed'))).ok
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is None
        router._registry.unregister_agent.assert_awaited_once()
        assert (await router.send_request(_request(rid='recovered'))).ok
        assert len(clients) == 2 and len(router.test_api.creates) == 2
        assert all(
            body['mounts'] == router.test_api.creates[0]['mounts'] for body in router.test_api.creates
        )
    finally:
        await router.shutdown()


@pytest.mark.parametrize('url', ['ftp://adx.example', 'https://user:secret@adx.example', 'https://adx.example/path',
                                  'https://adx.example?token=x', 'https://adx.example/#fragment'])
def test_invalid_ingress_origins_fail_at_startup(monkeypatch, url):
    with pytest.raises(ValueError):
        _config(monkeypatch, ingress_url=url)


@pytest.mark.asyncio
async def test_registry_launch_spec_is_forwarded_to_environment_without_template_registration(monkeypatch):
    from jiuwenswarm.extensions.agentos.agentos_router.registry_client import ImageInfo

    router, _clients = _router(_config(monkeypatch, builtin_image='registry.example/builtin:1'))
    spec = {'rootfs': {'imageurl': 'registry.example/third:2', 'user': '10001:10001', 'ports': ['tcp:8080']},
            'cmds': [['/app/worker', '--serve']], 'code_path': '/work', 'cpu': 500, 'memory': 1024,
            'probes': {'startup': {'tcpSocket': {'port': 8080}, 'failureThreshold': 3}}}
    router._registry.get_image_info = AsyncMock(return_value=ImageInfo(
        image_name='third', image_uri='registry.example/third:2',
        metadata={'runtime_spec': spec, 'env_vars': {'MODEL_ENDPOINT': 'http://model/v1'}}))
    await router.connect('')
    try:
        runtime = await router._agent_manager.get_or_create_agent('alice', 'third', creator=router._create_agent)
        body = router.test_api.creates[0]
        assert runtime.info.sandbox_id == 'test-' + body['name']
        assert body['name'] == 'alice+third-' + runtime.info.agent_id[:12]
        assert body['rootfs']['imageurl'] == 'registry.example/third:2'
        assert body['process'] == {'commands': spec['cmds'], 'user': '10001:10001', 'cwd': '/work'}
        assert body['cpu'] == 500 and body['memory'] == 1024
        assert body['env'] == {'MODEL_ENDPOINT': 'http://model/v1', AGENT_GENERATION_ENV: runtime.info.agent_id}
        assert body['mounts'][0]['host_path'].endswith('/alice')
        assert body['readinessProbe']['tcpSocket']['port'] == 8080
        assert all('/api/agent/' not in request.url.path for request in router.test_api.requests)
    finally:
        await router.shutdown()



@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.asyncio
async def test_native_placement_is_available_to_strict_registry_on_first_post(monkeypatch, existing):
    from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo
    from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryClient, RegistryConfig

    api = EnvironmentAPI()
    identifier = 'test-alice+jiuwenswarm'
    placement = {'id': identifier, 'tenant_id': 'appliance', 'status': 'running',
                 'node_ip': '172.30.58.2', 'sandbox_ip': '100.96.201.131'}
    if existing:
        api.environments[identifier] = dict(placement)

    def environment(request):
        response = api.handle(request)
        if request.method == 'POST':
            api.environments[identifier].update(placement)
        return response

    registered = []

    def strict_registry(request):
        body = json.loads(request.content)
        assert body['node'] == placement['node_ip'], 'Registry requires the real node at initial registration'
        assert body['address'] == placement['sandbox_ip']
        assert body['instance_id'] == identifier
        registered.append(body)
        return httpx.Response(200, json={**body, 'status': '运行'})

    control = AdxControlClient(_config(monkeypatch), transport=httpx.MockTransport(environment))
    registry = RegistryClient(RegistryConfig(endpoint='http://registry.example'))
    registry._http = httpx.AsyncClient(base_url='http://registry.example', transport=httpx.MockTransport(strict_registry))
    try:
        sandbox = await control.create_sandbox(namespace='test', name='alice+jiuwenswarm',
            workspace='/home/agentos/alice', runtime_spec={'rootfs': {'imageurl': 'agentos-supervisor:v1'}})
        await registry.register_agent(AgentInfo(user_id='alice', agent_type='jiuwenswarm',
            sandbox_id=sandbox.sandbox_id, metadata={'sandbox': sandbox.metadata}))
        assert len(registered) == 1
    finally:
        await control.close()
        await registry.close()


@pytest.mark.asyncio
async def test_config_request_retains_adx_warmup_failure(monkeypatch):
    from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentRuntime

    router, _ = _router(_config(monkeypatch))
    await router.connect('')
    started = asyncio.Event()
    release_create = asyncio.Event()
    joined = asyncio.Event()
    release_waiter = asyncio.Event()
    wait = AgentRuntime.wait_until_settled
    request = router._control.http.request
    detail = 'requested cgroup v1 backend is unavailable'

    async def create(method, url, **kwargs):
        if method == 'POST' and url.endswith('/environments'):
            started.set()
            await release_create.wait()
            return httpx.Response(409, json={
                'message': detail,
                'error': {'code': 'CONFLICT', 'outcome': 'terminal', 'requestId': 'create-test'},
            })
        return await request(method, url, **kwargs)

    async def wait_for_create(runtime, timeout):
        joined.set()
        await wait(runtime, timeout)
        await release_waiter.wait()

    monkeypatch.setattr(router._control.http, 'request', create)
    monkeypatch.setattr(AgentRuntime, 'wait_until_settled', wait_for_create)
    warmup = asyncio.create_task(router._warmup_user_agent('alice'))
    envelope = _request()
    envelope.method = 'config.get'
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending = asyncio.create_task(router.send_request(envelope))
        await asyncio.wait_for(joined.wait(), 2)
        release_create.set()
        await warmup
        release_waiter.set()
        response = await pending
        assert not response.ok
        assert 'AGENT_CREATE_FAILED' in response.payload['error']
        assert detail in response.payload['error']
        assert 'create-test' in response.payload['error']
    finally:
        release_create.set()
        release_waiter.set()
        await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("default_image", [None, "tools:v1"])
async def test_gateway_workspace_image_is_not_a_new_user_default(monkeypatch, default_image):
    router, _ = _router(_config(monkeypatch))
    sandbox = {"type": "adx-environment", "runtime": "runsc", "image": "workspace:v1",
        "rootfs_template": "local-workspace", "local_images": {
            "workspace:v1": "local-workspace", "tools:v1": "local-default"}}
    if default_image:
        sandbox["deployment_image"] = default_image
    router.set_or_update_server_config(config={"sandbox": sandbox})
    await router.connect("")
    try:
        assert (await router.send_request(_request())).ok
        deployment = json.loads(router.test_api.creates[0]["env"]["JIUWENSWARM_AGENTOS_SANDBOX_CONFIG"])
        assert deployment == {"type": "adx-environment"}
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_adx_gateway_does_not_broadcast_its_tool_ingress_to_another_node(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    router.set_or_update_server_config(config={"sandbox": {
        "enabled": True, "type": "adx-environment", "url": "https://master:8443",
        "startup_mode": "external", "image": "tools:v1", "runtime": "conch",
        "mounts": [{"host_path": "/opt/python", "target": "/opt/python", "options": ["ro"]}],
        "env": {"PATH": "/opt/python/bin:/usr/bin", "ADX_TOKEN": "not-for-tools", "JIUWENSWARM_ADX_NODE_CONTEXT_FILE": "/private-node.json"},
    }})
    await router.connect("")
    try:
        assert (await router.send_request(_request())).ok
        env = router.test_api.creates[0]["env"]
        deployment = json.loads(env["JIUWENSWARM_AGENTOS_SANDBOX_CONFIG"])
        assert "url" not in deployment
        assert "startup_mode" not in deployment
        assert "JIUWENSWARM_ADX_NODE_INGRESS_URL" not in env
        assert deployment == {"type": "adx-environment"}
    finally:
        await router.shutdown()


def test_managed_builtin_launch_uses_deployed_process_and_agent_only_token(monkeypatch):
    launch = {"process": {"commands": [["/opt/python/bin/python", "-m", "jiuwenswarm.server.app_agentserver"]]},
              "env": {"JIUWENSWARM_ADX_NODE_CONTEXT_FILE": "/run/agentos/node.json"},
              "mounts": [{"type": "bind", "host_path": "/opt/python", "target": "/opt/python", "options": ["ro"]}]}
    control = AdxControlClient(_config(monkeypatch, builtin_launch=launch))
    spec = {"rootfs": {"imageurl": "jiuwenswarm-agent-runtime:latest", "user": "agentos"}}
    agent = control._creation_body("adx", "alice+jiuwenswarm", "/users/alice", spec, {}, [])
    assert agent["process"] == launch["process"]
    assert agent["env"]["JIUWENSWARM_ADX_API_TOKEN"] == "platform-secret"
    assert not any(key.startswith("ADX_") for key in agent["env"])
    third = control._creation_body("adx", "other", "/users/alice", spec, {}, [])
    assert "ADX_TOKEN" not in third["env"]
    assert "JIUWENSWARM_ADX_API_TOKEN" not in third["env"]
    assert third['mounts'] == [{'type': 'bind', 'host_path': '/users/alice',
                               'target': control.config.workspace_target, 'options': ['rbind', 'rw']}]
    assert third.get('process') != launch['process']
    assert not any(m["target"] == "/run/agentos/node.json" for m in third["mounts"])


@pytest.mark.parametrize('runtime', ['runsc', 'supervisor'])
def test_builtin_home_matches_workspace_mount_for_each_runtime(monkeypatch, runtime):
    home = '/workspace/agent-home'
    launch = {'process': {'commands': [['python', '-m', 'jiuwenswarm.server.app_agentserver']]},
              'env': {'HOME': '/old/home', 'JIUWENSWARM_HOME': '/old/home'}}
    control = AdxControlClient(_config(monkeypatch, runtime_class=runtime, workspace_target=home,
                                     builtin_launch=launch))
    builtin = control._creation_body('adx', 'builtin', '/users/alice', {}, {}, [], builtin=True)
    assert builtin['env']['HOME'] == builtin['env']['JIUWENSWARM_HOME'] == home
    assert builtin['mounts'][0]['host_path'] == '/users/alice'
    assert builtin['mounts'][0]['target'] == home
    custom = control._creation_body('adx', 'custom', '/users/alice', {}, {'HOME': '/image/home'}, [], builtin=False)
    assert custom['env'] == {'HOME': '/image/home'}
    assert launch['env'] == {'HOME': '/old/home', 'JIUWENSWARM_HOME': '/old/home'}


def test_explicit_workspace_handle_option_applies_only_to_builtin_runsc(monkeypatch):
    options = ['rw', 'disable_file_handle_sharing']
    launch = {'process': {'commands': [['python', '-m', 'jiuwenswarm.server.app_agentserver']]},
              'workspace_mount_options': options}
    spec = {'rootfs': {'imageurl': 'jiuwenswarm-agent-runtime:latest'}}
    for runtime in ('runsc', 'supervisor'):
        control = AdxControlClient(_config(monkeypatch, runtime_class=runtime, builtin_launch=launch))
        builtin = control._creation_body('adx', 'builtin', '/users/alice', spec, {}, [], builtin=True)
        custom = control._creation_body('adx', 'custom', '/users/alice', spec, {}, [], builtin=False)
        expected = ['rbind', *options] if runtime == 'runsc' else ['rbind', 'rw']
        assert builtin['mounts'][0]['options'] == expected
        assert custom['mounts'][0]['options'] == ['rbind', 'rw']
    assert options == ['rw', 'disable_file_handle_sharing']


@pytest.mark.parametrize('options', [[], ['ro'], ['rw', 'ro'], ['rw', 'rw'], ['rw', 'unknown']])
def test_invalid_builtin_workspace_options_fail_before_create(monkeypatch, options):
    launch = {'process': {'commands': [['python']]}, 'workspace_mount_options': options}
    control = AdxControlClient(_config(monkeypatch, runtime_class='runsc', builtin_launch=launch))
    with pytest.raises(ValueError, match='workspace_mount_options'):
        control._creation_body('adx', 'builtin', '/users/alice', {}, {}, [], builtin=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('state,restart_pending', [('running',False),('pausing',False),('paused',False),('resuming',False),('failed',True)])
async def test_transport_failure_keeps_adx_lifecycle_and_checkpoint(monkeypatch, state, restart_pending):
    router, clients = _router(_config(monkeypatch, runtime_class='runsc'))
    await router.connect('')
    try:
        await router.send_request(_request())
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        row = router.test_api.environments[runtime.info.sandbox_id]
        row.update(status=state, restart_pending=restart_pending)
        await router._run_network_failure_delete(runtime, reason='connection_closed')
        assert runtime.info.sandbox_id in router.test_api.environments
        assert not any(r.method == 'DELETE' for r in router.test_api.requests)
        clients[0].disconnect.assert_awaited()
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is not None
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_uncertain_lifecycle_observation_does_not_delete(monkeypatch):
    from jiuwenswarm.extensions.adx.control_client import AdxControlError
    router, _ = _router(_config(monkeypatch, runtime_class='runsc'))
    await router.connect('')
    try:
        await router.send_request(_request())
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        monkeypatch.setattr(router._control, 'get_agent_info', AsyncMock(side_effect=AdxControlError('lookup timed out', outcome='unknown')))
        await router._run_network_failure_delete(runtime, reason='timeout')
        assert not any(r.method == 'DELETE' for r in router.test_api.requests)
    finally:
        await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime_class', ['supervisor', 'runsc'])
async def test_gateway_idle_deletes_environment_and_next_request_recreates(monkeypatch, runtime_class):
    router, clients = _router(_config(monkeypatch, runtime_class=runtime_class))
    await router.connect('')
    try:
        await router.send_request(_request())
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        router._agent_manager.increment_user_connections('alice')
        assert clients[0].server_ready
        router._agent_manager._runtimes[runtime.key].last_active_at = 0
        if runtime_class == 'runsc':
            assert router.test_api.creates[0]['idleTimeoutSeconds'] == 600
        assert await router._reap_idle_once() == 1
        assert runtime.info.sandbox_id not in router.test_api.environments
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is None
        assert any(r.method == 'DELETE' for r in router.test_api.requests)
        clients[0].disconnect.assert_awaited()
        response = await router.send_request(_request(rid='chat-after-idle'))
        assert response.ok
        assert len(router.test_api.creates) == 2
        replacement = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        assert replacement.info.sandbox_id != runtime.info.sandbox_id
        assert replacement.info.sandbox_id in router.test_api.environments
    finally:
        await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime_class', ['supervisor', 'runsc'])
async def test_gateway_idle_keeps_inflight_requests_and_restarts_clock_on_release(monkeypatch, runtime_class):
    router, _ = _router(_config(monkeypatch, runtime_class=runtime_class))
    await router.connect('')
    try:
        await router.send_request(_request())
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm', acquire=True)
        router._agent_manager._runtimes[runtime.key].last_active_at = 0
        assert await router._reap_idle_once() == 0
        assert runtime.info.sandbox_id in router.test_api.environments
        await router._agent_manager.release(runtime.key, expected_agent_id=runtime.info.agent_id)
        assert await router._reap_idle_once() == 0
        router._agent_manager._runtimes[runtime.key].last_active_at = 0
        assert await router._reap_idle_once() == 1
        assert runtime.info.sandbox_id not in router.test_api.environments
    finally:
        await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['unknown', 'invalid-json'])
async def test_unknown_create_reply_keeps_original_generation(monkeypatch, reply):
    router, _ = _router(_config(monkeypatch))
    api = router.test_api
    async def handle(request):
        response = api.handle(request)
        if request.method == 'POST':
            if reply == 'invalid-json':
                return httpx.Response(502, text='proxy lost the response')
            return httpx.Response(503, json={'message': 'create result unknown', 'error': {'code': 'UNAVAILABLE', 'outcome': 'unknown'}})
        return response
    await router._control.http.aclose()
    router._control.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    await router.connect('')
    try:
        first = await router.send_request(_request())
        assert not first.ok
        assert 'AGENT_REBUILD_PENDING' in first.payload['error']
        second = await router.send_request(_request())
        assert second.ok
        assert len(api.creates) == 1
    finally:
        await router.shutdown()


@pytest.mark.parametrize('runtime', ['supervisor', 'runsc'])
def test_builtin_sdk_credential_does_not_require_runsc_launch_override(monkeypatch, runtime):
    control = AdxControlClient(_config(monkeypatch, runtime_class=runtime))
    spec = {'rootfs': {'imageurl': 'jiuwenswarm-agent-runtime:latest', 'user': 'agentos'}}
    builtin = control._creation_body('adx', 'alice+jiuwenswarm', '/users/alice', spec, {}, [], builtin=True)
    assert builtin['env']['JIUWENSWARM_ADX_API_TOKEN'] == 'platform-secret'
    custom = control._creation_body('adx', 'alice+custom', '/users/alice', spec, {}, [], builtin=False)
    assert 'JIUWENSWARM_ADX_API_TOKEN' not in custom['env']


@pytest.mark.asyncio
@pytest.mark.parametrize('actual,accepted', [('expected', True), ('other', False)])
async def test_redacted_instance_generation_uses_authenticated_runtime_probe(monkeypatch, actual, accepted):
    router, _ = _router(_config(monkeypatch))
    instance = 'test-redacted-generation'
    router.test_api.environments[instance] = {'id': instance, 'tenant_id': 'appliance', 'status': 'running'}
    router._control.invoke = AsyncMock(return_value={'exit_code': 0, 'stdout': actual})
    try:
        if accepted:
            assert (await router._control.wait_until_running(
                instance, expected_generation='expected', trace_id='generation-probe',
            ))['id'] == instance
        else:
            with pytest.raises(YuanrongAgentGenerationMismatchError):
                await router._control.wait_until_running(
                    instance, expected_generation='expected', trace_id='generation-probe',
                )
        router._control.invoke.assert_awaited_once_with(instance, 'cmd.run', {
            'cmd': f'printf %s "${AGENT_GENERATION_ENV}"', 'timeout': 5,
        }, trace_id='generation-probe')
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_redacted_generation_probe_failure_does_not_prove_ownership(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    router._control.invoke = AsyncMock(return_value={'exit_code': 1, 'stdout': 'expected'})
    try:
        assert await router._control._instance_generation('test-candidate', {'status': 'running'}) is None
    finally:
        await router.shutdown()


@pytest.mark.parametrize('runtime', ['supervisor', 'runsc'])
def test_builtin_new_workspace_uses_prepared_read_only_template(monkeypatch, runtime):
    control = AdxControlClient(_config(monkeypatch, runtime_class=runtime,
                                     workspace_config_template='/etc/agentos/workspace.yaml'))
    body = control._creation_body('test', 'new-user', '/workspace', {}, None, None, builtin=True)
    assert {'type': 'bind', 'host_path': '/etc/agentos/workspace.yaml',
            'target': '/run/agentos/workspace-config.yaml', 'options': ['ro']} in body['mounts']
    assert {'type': 'bind', 'host_path': '/etc/agentos/workspace.env',
            'target': '/run/agentos/workspace-config.env', 'options': ['ro']} in body['mounts']
    assert body['env']['JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE'] == '/run/agentos/workspace-config.yaml'
    custom = control._creation_body('test', 'custom', '/workspace', {}, None, None, builtin=False)
    assert 'JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE' not in custom['env']
    assert all(m['host_path'] != '/etc/agentos/workspace.yaml' for m in custom['mounts'])
