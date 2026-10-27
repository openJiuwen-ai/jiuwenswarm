"""Public Binding management and file contracts through the ADX adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jiuwenswarm.extensions.adx.control_client import AdxControlClient, AdxControlError
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient, AdxRoutingError
from gateway.routing.container_files import AgentOSFileTransferError
from gateway.extensions.auth.credential_authenticator import AuthResult
from test_adx_router import _config, FakeClient, _request


async def test_binding_management_uses_public_ingress_and_checks_identity(monkeypatch):
    cfg = _config(monkeypatch)
    calls = []
    control = None

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.headers['Authorization'] == 'Bearer platform-secret'
        assert request.url.host == 'adx.example'
        if request.method == 'DELETE':
            return httpx.Response(200, json={'status': 'deleted'})
        return httpx.Response(200, json={'binding': dict(scope=control.scope('alice'),
            parameters={'workspace': 'old-value'}, generation='g1', phase='active', environment_id='adx-g1')})

    control = AdxControlClient(cfg, transport=httpx.MockTransport(handle))
    assert await control.binding_environment('alice') == 'adx-g1'
    await control.delete_binding('alice')
    assert [method for method, path in calls] == ['GET', 'DELETE']
    assert all(path == '/api/agent/v2/templates/jiuwen/versions/v1/bindings/' + cfg.binding_id('alice')
               for method, path in calls)
    assert control.parameters('alice')['workspace'] == 'w-6164fae0b0fdc6ed18d42ec39ad28dad2ea54b7e955664c78429007ccdeb50e3'
    assert control.parameters('alice') != control.parameters('bob')
    with pytest.raises(AdxControlError, match='identity'):
        await control.binding_environment('bob')
    await control.close()


@pytest.mark.parametrize('failure', ['unknown', 'conflict', 'invalid'])
async def test_management_failure_is_not_replayed(monkeypatch, failure):
    calls = []

    def handle(request):
        calls.append(request.method)
        if failure == 'unknown':
            raise httpx.ReadTimeout('unknown')
        if failure == 'invalid':
            return httpx.Response(200, json=['invalid'])
        return httpx.Response(409, json={'kind': 'conflict'})

    control = AdxControlClient(_config(monkeypatch), transport=httpx.MockTransport(handle))
    with pytest.raises(AdxControlError):
        await control.binding_environment('alice')
    assert calls == ['GET']
    await control.close()


def _files(monkeypatch):
    control = AsyncMock()
    control.scope = lambda user: {'user': user}
    control.parameters = lambda user: {'workspace': 'w-' + user}
    control.environments = {}
    control.binding_environment.side_effect = lambda user: control.environments.get(user, 'adx-' + user)
    router = AdxRouterClient(_config(monkeypatch), ws_client_factory=FakeClient, control_client=control)
    return router, control


def _file_channel(router, *, with_auth=True):
    from gateway.channel_manager.base import RobotMessageRouter
    from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

    async def authenticate(*, headers, path, remote, channel):
        if headers.get('authorization') != 'Bearer business-test-token':
            return AuthResult(False, error='Invalid business credential')
        return AuthResult(True, user_id='alice')

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter(), agent_client=router)
    if with_auth:
        channel.set_handshake_auth(authenticate)
    router.set_channel_manager(SimpleNamespace(
        get_channel=lambda kind: channel if kind.value == 'web' else None,
        subscribe_channel_events=lambda callback: None,
    ))
    return channel


@pytest.mark.parametrize('connected', [True, False])
async def test_gateway_health_check_is_mocked_without_activation(monkeypatch, connected):
    from gateway.health_check import (
        HEALTH_CHECK_OK,
        GatewayHealthCheckService,
        HealthCheckConfig,
    )

    router, control = _files(monkeypatch)
    router._factory = lambda: pytest.fail('Health check must not open a user connection')
    messages = SimpleNamespace(publish_robot_messages=AsyncMock())
    service = GatewayHealthCheckService(
        router,
        HealthCheckConfig(interval_seconds=60, timeout_seconds=1, relay_channel_id='web'),
        message_handler=messages,
    )
    try:
        if connected:
            await router.connect('')
        await service._tick()
        assert service.last_tick_ok is connected
        if connected:
            message = messages.publish_robot_messages.call_args.args[0]
            assert HEALTH_CHECK_OK in message.payload['health_check']
            assert 'mock' in message.payload['health_check']
        else:
            messages.publish_robot_messages.assert_not_awaited()
        control.binding_environment.assert_not_awaited()
        control.invoke.assert_not_awaited()
    finally:
        await router.shutdown()


async def test_file_upload_is_atomic_and_scoped_to_activated_user(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    payload = b'hello' * 17000
    actions = []
    def invoke(env, action, args):
        assert env == 'adx-alice'
        actions.append((action, args))
        return {'bytes_written': len(bytes.fromhex(args['data']))} if action == 'file.write_chunk' else {}
    control.invoke.side_effect = invoke
    result = await router.upload_container_file(user_id='alice', path='probe.txt', content=payload)
    writes = [a for k, a in actions if k == 'file.write_chunk']
    assert b''.join(bytes.fromhex(a['data']) for a in writes) == payload
    assert [a['offset'] for a in writes] == [0, 65536]
    assert actions[-2][0] == 'file.rename' and actions[-2][1]['new_path'] == '/home/agentos/probe.txt'
    assert result['size'] == len(payload)
    with pytest.raises(AgentOSFileTransferError):
        await router.download_container_file(user_id='bob', path='/home/agentos/probe.txt', instance_id='adx-alice')
    await router.shutdown()


@pytest.mark.parametrize('replace_during_stream', [False, True])
async def test_http_download_keeps_adapter_scope_across_streaming_tasks(monkeypatch, replace_during_stream):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')

    def invoke(env, action, args):
        if action == 'file.stat':
            return {'type': 'file', 'size': 6}
        if replace_during_stream and env == 'adx-alice' and args['offset'] == 0:
            control.environments['alice'] = 'adx-replaced'
        return {'data': (b'abc' if args['offset'] == 0 else b'def').hex(), 'bytes_read': 3}

    control.invoke.side_effect = invoke
    channel = _file_channel(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)), base_url='http://gateway', headers={'Authorization': 'Bearer business-test-token'}) as client:
        if replace_during_stream:
            with pytest.raises(AgentOSFileTransferError, match='changed'):
                await client.get('/file-api/raw-file', params={'path': '/home/agentos/a', 'user_id': 'alice'})
            assert control.invoke.await_count == 2
        else:
            response = await client.get('/file-api/raw-file', params={'path': '/home/agentos/a', 'user_id': 'alice'})
            assert response.status_code == 200 and response.content == b'abcdef'
        control.environments['alice'] = 'adx-replaced'
        control.invoke.reset_mock()
        response = await client.get('/file-api/raw-file', params={'path': '/home/agentos/a', 'user_id': 'alice'})
        assert response.status_code == 200 and response.content == b'abcdef'
        assert all(call.args[0] == 'adx-replaced' for call in control.invoke.await_args_list)
    await router.shutdown()


async def test_rebuild_closes_old_socket_and_recreates_binding_on_next_request(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    await router.send_request(_request())
    old = router._clients['alice']
    gate = asyncio.Event()
    async def delete(*args):
        await gate.wait()
    control.delete_binding.side_effect = delete
    task = asyncio.create_task(router.delete_agent('alice'))
    await asyncio.sleep(0)
    with pytest.raises(AdxRoutingError, match='rebuilt'):
        await router.send_request(_request())
    with pytest.raises(AdxRoutingError):
        await router.delete_agent('alice')
    gate.set()
    assert await task == {'success': True}
    assert not old.server_ready
    control.delete_binding.assert_awaited_once_with('alice')
    await router.send_request(_request())
    assert router._clients['alice'] is not old
    assert old.headers == router._clients['alice'].headers
    await router.shutdown()


async def test_http_file_routes_require_business_auth_and_reject_other_users(monkeypatch):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.side_effect = lambda env, action, args: (
        {'entries': [{'name': 'a.txt', 'path': '/home/agentos/a.txt', 'type': 'file', 'size': 3}]}
        if action == 'file.list' else
        {'type': 'file', 'size': 3} if action == 'file.stat' else
        {'data': b'abc'.hex(), 'bytes_read': 3, 'eof': True})
    channel = _file_channel(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)), base_url='http://gateway', headers={'Authorization': 'Bearer business-test-token'}) as client:
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'alice'})
        assert response.status_code == 200 and 'a.txt' in response.text
        response = await client.get('/file-api/raw-file', params={'path': '/home/agentos/a.txt', 'user_id': 'alice'})
        assert response.status_code == 200 and response.content == b'abc'
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'bob'})
        assert response.status_code == 403 and response.json()['code'] == 'USER_MISMATCH'
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'alice'},
            headers={'X-User-Id': 'bob'})
        assert response.status_code == 403
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'alice'},
            headers={'Authorization': 'Bearer unrelated-platform-token'})
        assert response.status_code == 401
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos'})
        assert response.status_code == 400
    assert [call.args[0] for call in control.invoke.await_args_list] == ['adx-alice', 'adx-alice', 'adx-alice']
    await router.shutdown()


@pytest.mark.parametrize('route, params', [
    ('list-files', {'dir': '/home/agentos'}),
    ('raw-file', {'path': '/home/agentos/probe.txt'}),
])
@pytest.mark.parametrize('failure, status, code', [
    ('agent_type', 400, 'BAD_REQUEST'),
    ('connect', 502, 'ADX_CONNECT_FAILED'),
    ('binding', 502, 'SERVICE_UNAVAILABLE'),
])
async def test_http_file_routes_map_adx_failures(monkeypatch, route, params, failure, status, code):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    params = {**params, 'user_id': 'alice'}
    if failure == 'agent_type':
        params['agent_type'] = 'unsupported'
    elif failure == 'connect':
        monkeypatch.setattr(FakeClient, 'connect', AsyncMock(side_effect=OSError('unreachable')))
    else:
        control.binding_environment.side_effect = AdxControlError('ADX Binding is unavailable')
    channel = _file_channel(router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_web_channel_app(channel)), base_url='http://gateway',
            headers={'Authorization': 'Bearer business-test-token'},
        ) as client:
            response = await client.get('/file-api/' + route, params=params)
        assert response.status_code == status
        assert response.json()['code'] == code
        control.invoke.assert_not_awaited()
    finally:
        await router.shutdown()


async def test_missing_runtime_file_preserves_existing_not_found_contract(monkeypatch):
    control = AdxControlClient(_config(monkeypatch), transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={'error': 'path not found: /home/agentos/probe.txt'})))
    with pytest.raises(AdxControlError) as caught:
        await control.invoke('adx-alice', 'file.stat', {'path': '/home/agentos/probe.txt'})
    assert caught.value.code == 'NOT_FOUND'
    await control.close()


async def test_recursive_listing_preserves_unlimited_depth_contract(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    boundary = '/home/agentos/' + '/'.join(['d'] * 64)
    def invoke(env, action, args):
        assert action == 'file.list' and args['depth'] == 64
        if args['path'] == '/home/agentos':
            return {'entries': [{'path': boundary, 'type': 'dir'}]}
        assert args['path'] == boundary
        return {'entries': [{'path': boundary + '/deep.txt', 'type': 'file'}]}
    control.invoke.side_effect = invoke
    result = await router.list_container_files(user_id='alice', dir_path='/home/agentos', recursive=True, max_depth=0)
    assert control.invoke.await_count == 2 and result[-1]['path'].endswith('/deep.txt')
    await router.shutdown()


async def test_first_ws_supplies_rendering_parameters_without_management_calls(monkeypatch):
    from dataclasses import replace
    from urllib.parse import parse_qs

    cfg = replace(_config(monkeypatch), parameters={'model_api_base': 'https://model.example/v1?a=b&c=d'})

    def unexpected(request):
        raise AssertionError('WS activation must not make a preceding management request')

    control = AdxControlClient(cfg, transport=httpx.MockTransport(unexpected))
    router = AdxRouterClient(cfg, ws_client_factory=FakeClient, control_client=control)
    await router.connect('')
    router.set_or_update_server_config(config={}, env={'API_KEY': 'not-a-rendering-parameter'})
    await router.send_request(_request())
    values = parse_qs(router._clients['alice'].headers['X-ADX-Parameters'])
    assert values['model_api_base'] == ['https://model.example/v1?a=b&c=d']
    assert values['workspace'] == [control.parameters('alice')['workspace']]
    assert 'API_KEY' not in values
    await router.shutdown()


@pytest.mark.parametrize('auth_result', [None, AuthResult(True)])
async def test_file_routes_fail_closed_without_authenticated_business_identity(monkeypatch, auth_result):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    channel = _file_channel(router, with_auth=False)
    if auth_result is not None:
        channel.set_handshake_auth(AsyncMock(return_value=auth_result))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_web_channel_app(channel)), base_url='http://gateway',
    ) as client:
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'alice'})
    assert response.status_code == 401
    control.invoke.assert_not_awaited()
    assert router._clients == {}
    await router.shutdown()


@pytest.mark.parametrize('recursive', [False, True])
async def test_mkdir_uses_native_adx_action_and_default_mode(monkeypatch, recursive):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'created': True, 'error': None}
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/nested/dir', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/nested/dir', 'created': True}
    control.invoke.assert_awaited_once_with('adx-alice', 'file.mkdir', {'path': '/home/agentos/nested/dir'})
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/other', mode='700', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/other', 'created': True}
    control.invoke.assert_awaited_with('adx-alice', 'file.mkdir', {'path': '/home/agentos/other'})
    assert control.invoke.await_count == 2
    await router.shutdown()
