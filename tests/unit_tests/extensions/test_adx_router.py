# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway business routing over public ADX Environment lifecycle APIs."""
import asyncio
import base64
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse
from gateway.extensions.adx.config import load_router_config
from gateway.extensions.adx.control_client import AdxControlClient
from gateway.extensions.adx.router_client import AdxRouterClient


def _config(monkeypatch, **overrides):
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
    }})


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
            self.environments[instance] = {'id': instance, 'tenant_id': self.tenant, 'status': 'running'}
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


def _request(user='alice', rid='same-id', **params):
    return E2AEnvelope(request_id=rid, user_id=user, channel='web', method='chat.send', params=params)


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
        mounts = [body['mounts'][0]['host_path'] for body in router.test_api.creates]
        assert len(set(mounts)) == 2 and all(Path(path).is_dir() for path in mounts)
        assert all(body['process']['commands'][0][-1].startswith('exec jiuwenswarm-agentserver')
                   for body in router.test_api.creates)
    finally:
        await router.shutdown()

@pytest.mark.parametrize('remote_deleted', [False, True])
async def test_unknown_delete_retains_operation_until_lookup_confirms_result(monkeypatch, remote_deleted):
    router, clients = _router(_config(monkeypatch))
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
        create_operation = router._control._creates[instance][0]
        delete_operation = router._control._deletes[instance]
        clients[0].disconnect.assert_awaited_once()
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is None
        router._registry.unregister_agent.assert_awaited_once()
        # The delete may not have reached ADX. Stable-ID lookup reuses that instance.
        assert (await router.send_request(_request(rid='next'))).ok
        assert clients[-1].uri == first_url
        expected_creates = 2 if remote_deleted else 1
        assert len(router.test_api.creates) == expected_creates
        create_ids = [request.headers['x-request-id'] for request in router.test_api.requests
                      if request.method == 'POST']
        assert len(set(create_ids)) == expected_creates
        if remote_deleted:
            assert instance not in router._control._deletes
            assert router._control._creates[instance][0] != create_operation
        else:
            assert router._control._deletes[instance] == delete_operation
        router.test_api.delete_error = False
        assert await router.delete_agent('alice', 'jiuwenswarm')
        delete_ids = [request.headers['x-request-id'] for request in router.test_api.requests
                      if request.method == 'DELETE']
        if remote_deleted:
            assert delete_ids[-1] != delete_operation
        else:
            assert delete_ids[-1] == delete_operation
        assert instance not in router._control._deletes
        assert (await router.send_request(_request(rid='after-delete'))).ok
        assert len(router.test_api.creates) == expected_creates + 1
    finally:
        await router.shutdown()


@pytest.mark.parametrize('status', ['failed', 'deleted', 'deleting', 'missing'])
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


@pytest.mark.parametrize('url', ['http://adx.example', 'https://user:secret@adx.example', 'https://adx.example/path',
                                  'https://adx.example?token=x', 'https://adx.example/#fragment'])
def test_invalid_ingress_origins_fail_at_startup(monkeypatch, url):
    with pytest.raises(ValueError):
        _config(monkeypatch, ingress_url=url)


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
        assert runtime.info.sandbox_id == 'test-alice+third'
        body = router.test_api.creates[0]
        assert body['rootfs']['imageurl'] == 'registry.example/third:2'
        assert body['process'] == {'commands': spec['cmds'], 'user': '10001:10001', 'cwd': '/work'}
        assert body['cpu'] == 500 and body['memory'] == 1024
        assert body['env'] == {'MODEL_ENDPOINT': 'http://model/v1'}
        assert body['mounts'][0]['host_path'].endswith('/alice')
        assert body['readinessProbe']['tcpSocket']['port'] == 8080
        assert all('/api/agent/' not in request.url.path for request in router.test_api.requests)
    finally:
        await router.shutdown()
