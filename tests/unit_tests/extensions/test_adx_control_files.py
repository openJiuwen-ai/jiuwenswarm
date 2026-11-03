"""Environment-scoped file contracts through the ADX adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.extensions.adx.control_client import AdxControlClient, AdxControlError
from gateway.extensions.adx.router_client import AdxRouterClient, AdxRoutingError
from gateway.routing.container_files import AgentOSFileTransferError
from gateway.extensions.auth.credential_authenticator import AuthResult
from test_adx_router import _config, FakeClient, _request, _router


def _files(monkeypatch, *, auth_client=None):
    router, _clients = _router(_config(monkeypatch), auth_client=auth_client)
    router._control.invoke = AsyncMock()
    return router, router._control


def _file_channel(router):
    from gateway.channel_manager.base import RobotMessageRouter
    from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter(), agent_client=router)
    router.set_channel_manager(SimpleNamespace(
        get_channel=lambda kind: channel if kind.value == 'web' else None,
        subscribe_channel_events=lambda callback: None,
    ))
    return channel


def _business_authenticator():
    async def authenticate(context):
        if context.headers.get('authorization') != 'Bearer business-test-token':
            return AuthResult(False, error='Invalid business credential')
        return AuthResult(True, user_id='iam-alice', extensions={'username': 'alice'})

    return SimpleNamespace(authenticate=AsyncMock(side_effect=authenticate))


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
        assert router.test_api.requests == []
        control.invoke.assert_not_awaited()
    finally:
        await router.shutdown()


async def test_file_upload_is_atomic_and_scoped_to_activated_user(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    payload = b'hello' * 17000
    actions = []
    def invoke(env, action, args):
        assert env == 'test-alice+jiuwenswarm'
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
        await router.download_container_file(user_id='bob', path='/home/agentos/probe.txt', instance_id='test-alice+jiuwenswarm')
    await router.shutdown()


async def test_http_download_keeps_environment_and_releases_activity(monkeypatch):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    def invoke(env, action, args):
        assert env == 'test-alice+jiuwenswarm'
        if action == 'file.stat':
            return {'type': 'file', 'size': 6}
        return {'data': (b'abc' if args['offset'] == 0 else b'def').hex(), 'bytes_read': 3}
    control.invoke.side_effect = invoke
    channel = _file_channel(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)),
                                    base_url='http://gateway', headers={'Authorization': 'Bearer business-test-token'}) as client:
            response = await client.get('/file-api/raw-file', params={'path': '/home/agentos/a', 'user_id': 'alice'})
            assert response.status_code == 200 and response.content == b'abcdef'
        assert len(router.test_api.creates) == 1
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        assert runtime.task_count == 0
    finally:
        await router.shutdown()


async def test_http_file_routes_require_business_auth_and_reject_other_users(monkeypatch):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app
    auth_client = _business_authenticator()
    router, control = _files(monkeypatch, auth_client=auth_client)
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
        assert response.status_code == 200
    assert [call.args[0] for call in control.invoke.await_args_list] == [
        'test-alice+jiuwenswarm', 'test-alice+jiuwenswarm',
        'test-alice+jiuwenswarm', 'test-alice+jiuwenswarm',
    ]
    assert auth_client.authenticate.await_count == 6
    await router.shutdown()


@pytest.mark.parametrize('route, params', [
    ('list-files', {'dir': '/home/agentos'}),
    ('raw-file', {'path': '/home/agentos/probe.txt'}),
])
@pytest.mark.parametrize('failure, status, code', [
    ('agent_type', 404, 'instance_not_found'),
    ('environment', 502, 'SERVICE_UNAVAILABLE'),
])
async def test_http_file_routes_map_adx_failures(monkeypatch, route, params, failure, status, code):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    params = {**params, 'user_id': 'alice'}
    if failure == 'agent_type':
        params['agent_type'] = 'unsupported'
    else:
        control.create_sandbox = AsyncMock(side_effect=AdxControlError('ADX Environment unavailable'))
    channel = _file_channel(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)),
                                    base_url='http://gateway', headers={'Authorization': 'Bearer business-test-token'}) as client:
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
        await control.invoke('test-alice+jiuwenswarm', 'file.stat', {'path': '/home/agentos/probe.txt'})
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


async def test_file_routes_follow_original_auth_disabled_behavior(monkeypatch):
    from gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'entries': []}
    channel = _file_channel(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_web_channel_app(channel)), base_url='http://gateway',
    ) as client:
        response = await client.get('/file-api/list-files', params={'dir': '/home/agentos', 'user_id': 'alice'})
    assert response.status_code == 200
    control.invoke.assert_awaited_once()
    await router.shutdown()


@pytest.mark.parametrize('recursive', [False, True])
async def test_mkdir_uses_native_adx_action_and_default_mode(monkeypatch, recursive):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'created': True, 'error': None}
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/nested/dir', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/nested/dir', 'created': True}
    control.invoke.assert_awaited_once_with('test-alice+jiuwenswarm', 'file.mkdir', {'path': '/home/agentos/nested/dir'})
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/other', mode='700', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/other', 'created': True}
    control.invoke.assert_awaited_with('test-alice+jiuwenswarm', 'file.mkdir', {'path': '/home/agentos/other'})
    assert control.invoke.await_count == 2
    await router.shutdown()
