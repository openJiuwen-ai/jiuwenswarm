"""Environment-scoped file contracts through the ADX adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jiuwenswarm.extensions.adx.control_client import AdxControlClient, AdxControlError
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient, AdxRoutingError
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSFileTransferError
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from test_adx_router import _config, FakeClient, _request, _router


def _files(monkeypatch, *, auth_client=None):
    router, _clients = _router(_config(monkeypatch), auth_client=auth_client)
    router._control.invoke = AsyncMock()
    api = router.test_api
    original = api.handle
    api.transfers = []
    api.file_data = b'abcdef'
    async def handle(request):
        if request.url.path.endswith('/upload'):
            assert request.headers['authorization'] == 'Bearer platform-secret'
            content = await request.aread()
            api.transfers.append((request, content))
            return httpx.Response(200, json={'size': len(content)})
        if request.url.path.endswith('/download'):
            assert request.headers['authorization'] == 'Bearer platform-secret'
            api.transfers.append((request, None))
            content = api.file_data
            if 'range' in request.headers:
                start, end = map(int, request.headers['range'][6:].split('-'))
                return httpx.Response(206, content=content[start:end + 1], headers={
                    'Content-Range': f'bytes {start}-{end}/{len(content)}',
                })
            response = httpx.Response(200, content=content)
            if getattr(api, 'omit_download_length', False):
                del response.headers['Content-Length']
            return response
        return original(request)
    router._control.http._transport = httpx.MockTransport(handle)
    return router, router._control


def _alice_instance(router):
    creates = [body for body in router.test_api.creates
               if body['mounts'][0]['host_path'].endswith('/alice')]
    assert len(creates) == 1
    body = creates[0]
    return body['namespace'] + '-' + body['name']


def _file_channel(router):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

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
@pytest.mark.asyncio
async def test_gateway_health_check_is_mocked_without_activation(monkeypatch, connected):
    from jiuwenswarm.gateway.health_check import (
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


@pytest.mark.asyncio
async def test_file_upload_is_atomic_and_scoped_to_activated_user(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    payload = b'hello' * 17000
    actions = []
    def invoke(env, action, args):
        assert env == _alice_instance(router)
        actions.append((action, args))
        return {'bytes_written': len(bytes.fromhex(args['data']))} if action == 'file.write_chunk' else {'exit_code': 0}
    control.invoke.side_effect = invoke
    result = await router.upload_container_file(user_id='alice', path='probe.txt', content=payload)
    assert len(router.test_api.transfers) == 1
    request, uploaded = router.test_api.transfers[0]
    assert uploaded == payload
    assert request.url.path.endswith('/upload')
    assert not any(action == 'file.write_chunk' for action, _ in actions)
    assert actions[-2][0] == 'file.rename' and actions[-2][1]['new_path'] == '/home/agentos/probe.txt'
    assert result['size'] == len(payload)
    with pytest.raises(AgentOSFileTransferError):
        await router.download_container_file(user_id='bob', path='/home/agentos/probe.txt', instance_id=_alice_instance(router))
    await router.shutdown()


@pytest.mark.asyncio
async def test_http_download_keeps_environment_and_releases_activity(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    def invoke(env, action, args):
        assert env == _alice_instance(router)
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


@pytest.mark.asyncio
async def test_http_file_routes_require_business_auth_and_reject_other_users(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
    auth_client = _business_authenticator()
    router, control = _files(monkeypatch, auth_client=auth_client)
    await router.connect('')
    control.invoke.side_effect = lambda env, action, args: (
        {'entries': [{'name': 'a.txt', 'path': '/home/agentos/a.txt', 'type': 'file', 'size': 3}]}
        if action == 'file.list' else
        {'type': 'file', 'size': 3} if action == 'file.stat' else
        {'data': b'abc'.hex(), 'bytes_read': 3, 'eof': True})
    channel = _file_channel(router)
    router.test_api.file_data = b'abc'
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
    assert all(call.args[0] == _alice_instance(router) for call in control.invoke.await_args_list)
    assert len(router.test_api.transfers) == 1
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
@pytest.mark.asyncio
async def test_http_file_routes_map_adx_failures(monkeypatch, route, params, failure, status, code):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app

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


@pytest.mark.asyncio
async def test_missing_runtime_file_preserves_existing_not_found_contract(monkeypatch):
    control = AdxControlClient(_config(monkeypatch), transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={'error': 'path not found: /home/agentos/probe.txt'})))
    with pytest.raises(AdxControlError) as caught:
        await control.invoke('test-alice+jiuwenswarm', 'file.stat', {'path': '/home/agentos/probe.txt'})
    assert caught.value.code == 'file_not_found'
    await control.close()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_file_routes_follow_original_auth_disabled_behavior(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app

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
@pytest.mark.asyncio
async def test_mkdir_uses_native_adx_action_and_default_mode(monkeypatch, recursive):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.side_effect = lambda env, action, args: (
        {'created': True, 'error': None} if action == 'file.mkdir' else {'exit_code': 0}
    )
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/nested/dir', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/nested/dir', 'created': True}
    control.invoke.assert_awaited_once_with(_alice_instance(router), 'file.mkdir', {'path': '/home/agentos/nested/dir'})
    assert await router.mkdir_container_dir(
        user_id='alice', path='/home/agentos/other', mode='700', recursive=recursive,
    ) == {'success': True, 'path': '/home/agentos/other', 'created': True}
    assert control.invoke.await_args_list[-2].args[1:] == ('file.mkdir', {'path': '/home/agentos/other'})
    assert control.invoke.await_args_list[-1].args[1] == 'cmd.run'
    assert 'chmod -- 700 /home/agentos/other' in control.invoke.await_args_list[-1].args[2]['cmd']
    assert control.invoke.await_count == 3
    await router.shutdown()


@pytest.mark.asyncio
async def test_uploaded_file_and_new_parents_inherit_workspace_owner(monkeypatch):
    import shlex

    router, control = _files(monkeypatch)
    await router.connect('')
    actions = []
    def invoke(env, action, args):
        actions.append((action, args))
        if action == 'file.exists':
            return {'exists': args['path'] == '/home/agentos/existing'}
        if action == 'file.write_chunk':
            return {'bytes_written': len(bytes.fromhex(args['data']))}
        return {'exit_code': 0}
    control.invoke.side_effect = invoke
    try:
        await router.upload_container_file(
            user_id='alice', path="existing/new dir/it's a file.txt", content=b'owned')
        commands = [args['cmd'] for action, args in actions if action == 'cmd.run']
        assert len(commands) == 1
        words = shlex.split(commands[0])
        assert words[:4] == ['chown', '-h', '--reference=/home/agentos', '--']
        assert '/home/agentos/existing/new dir' in words[4:]
        assert '/home/agentos/existing' not in words[4:]
        assert actions[-3][0] == 'cmd.run' and actions[-2][0] == 'file.rename'
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_upload_does_not_publish_when_owner_cannot_be_applied(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    def invoke(env, action, args):
        if action == 'file.exists':
            return {'exists': True}
        if action == 'file.write_chunk':
            return {'bytes_written': len(bytes.fromhex(args['data']))}
        if action == 'cmd.run':
            return {'exit_code': 1, 'stderr': 'Operation not permitted'}
        return {}
    control.invoke.side_effect = invoke
    try:
        with pytest.raises(AgentOSFileTransferError):
            await router.upload_container_file(user_id='alice', path='probe.txt', content=b'owned')
        assert not any(call.args[1] == 'file.rename' for call in control.invoke.await_args_list)
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_native_download_holds_runtime_until_stream_closed(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'type': 'file', 'size': 6}
    async with router.open_container_file(user_id='alice', path='/home/agentos/a') as response:
        runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
        assert runtime.task_count == 1
        assert await response.aread() == b'abcdef'
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    assert runtime.task_count == 0
    control.invoke.assert_awaited_once_with(_alice_instance(router), 'file.stat', {'path': '/home/agentos/a'})
    await router.shutdown()


@pytest.mark.asyncio
async def test_native_range_download_preserves_chunk_contract(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'type': 'file', 'size': 6}
    result = await router.download_container_file(user_id='alice', path='/home/agentos/a', offset=2, limit=3)
    assert result.data == b'cde' and result.offset == 2 and result.size == 6 and not result.eof
    assert router.test_api.transfers[0][0].headers['range'] == 'bytes=2-4'
    result = await router.download_container_file(user_id='alice', path='/home/agentos/a', offset=6)
    assert result.eof and result.data == b'' and len(router.test_api.transfers) == 1
    await router.shutdown()


@pytest.mark.asyncio
async def test_interrupted_upload_never_publishes_target_and_releases_runtime(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'exists': True, 'exit_code': 0}
    async def chunks():
        yield b'partial'
        raise RuntimeError('client disconnected')
    with pytest.raises(RuntimeError, match='client disconnected'):
        await router.upload_container_file_stream(user_id='alice', path='a', chunks=chunks())
    assert not any(call.args[1] == 'file.rename' for call in control.invoke.await_args_list)
    assert control.invoke.await_args_list[-1].args[1] == 'file.remove'
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    assert runtime.task_count == 0
    await router.shutdown()


@pytest.mark.asyncio
async def test_native_upload_rejects_traversal_before_creating_environment(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    with pytest.raises(AgentOSFileTransferError):
        await router.upload_container_file(user_id='alice', path='../escape', content=b'bad')
    assert not router.test_api.creates and not router.test_api.transfers
    await router.shutdown()


@pytest.mark.asyncio
async def test_native_attachment_upload_prepares_on_same_runtime(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentResponse
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'exists': True, 'exit_code': 0}
    await router._resolve_file_runtime(user_id='alice')
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    ws = await router._get_ws_client(runtime)
    async def prepare(envelope, **kwargs):
        assert envelope.params['prepare_only'] is True and envelope.params['data'] == ''
        return AgentResponse(envelope.request_id, envelope.channel,
                             payload={'path': '/home/agentos/.jiuwenswarm/agent/sessions/s/uploads/a.txt'})
    ws.send_request = AsyncMock(side_effect=prepare)
    result = await router.upload_attachment(b'attachment', 'agent/sessions/s/uploads/a.txt', user_id='alice', session_id='s')
    assert result['size'] == 10
    assert router.test_api.transfers[0][1] == b'attachment'
    assert router.test_api.transfers[0][0].url.path == '/direct/' + _alice_instance(router) + '/upload'
    assert not any(call.args[1] == 'file.write_chunk' for call in control.invoke.await_args_list)
    await router.shutdown()


@pytest.mark.asyncio
async def test_native_http_disconnect_closes_upstream_and_releases_activity(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.container_file_http import _native_file_response
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'type': 'file', 'size': 6}
    response = await _native_file_response(
        router, request=SimpleNamespace(headers={}, method='GET'), user_id='alice',
        path='/home/agentos/a', session_id='',
    )
    async def send(_message):
        raise OSError('browser disconnected')
    async def receive():
        return {'type': 'http.disconnect'}
    with pytest.raises(Exception):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    assert runtime.task_count == 0
    await router.shutdown()


@pytest.mark.asyncio
async def test_retained_agentserver_uses_legacy_protocol_only_before_a_write(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.gateway.routing.agent_http_bridge import upload_file_bytes_via_e2a
    from jiuwenswarm.gateway.routing import e2a_proxy
    router, control = _files(monkeypatch)
    await router.connect('')
    await router._resolve_file_runtime(user_id='alice')
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    ws = await router._get_ws_client(runtime)
    ws.send_request = AsyncMock(side_effect=lambda envelope: AgentResponse(
        envelope.request_id, envelope.channel, ok=False, payload={'error': 'data is empty', 'code': 'BAD_REQUEST'},
    ))
    legacy = AsyncMock(return_value=(True, {'path': '/home/agentos/a', 'size': 3}))
    monkeypatch.setattr(e2a_proxy, 'fetch_agent_unary', legacy)
    ok, result = await upload_file_bytes_via_e2a(
        b'old', 'a', agent_client=router, user_id='alice', channel_id='web', session_id='s',
    )
    assert ok and result['size'] == 3
    legacy.assert_awaited_once()
    assert not router.test_api.transfers
    ws.send_request = AsyncMock(side_effect=lambda envelope: AgentResponse(
        envelope.request_id, envelope.channel, ok=False, payload={'error': 'path_outside_workspace', 'code': 'BAD_REQUEST'},
    ))
    ok, _ = await upload_file_bytes_via_e2a(
        b'old', 'a', agent_client=router, user_id='alice', channel_id='web', session_id='s',
    )
    assert not ok and legacy.await_count == 1
    await router.shutdown()


@pytest.mark.asyncio
async def test_native_head_uses_business_auth_and_releases_stream(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
    router, control = _files(monkeypatch, auth_client=_business_authenticator())
    await router.connect('')
    router.test_api.omit_download_length = True
    control.invoke.return_value = {'type': 'file', 'size': 6}
    channel = _file_channel(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)),
                                base_url='http://gateway') as client:
        params = {'path': '/home/agentos/a', 'user_id': 'alice'}
        response = await client.head('/file-api/raw-file', params=params,
                                     headers={'Authorization': 'Bearer business-test-token'})
        assert response.status_code == 200 and response.content == b''
        assert response.headers['Content-Length'] == '6'
        response = await client.head('/file-api/raw-file', params=params)
        assert response.status_code == 401
    assert len(router.test_api.transfers) == 1
    runtime = await router._agent_manager.get_agent('alice', 'jiuwenswarm')
    assert runtime.task_count == 0
    await router.shutdown()


@pytest.mark.asyncio
async def test_upload_limit_counts_stream_and_never_publishes_oversize(monkeypatch):
    from jiuwenswarm.extensions.adx import file_client
    monkeypatch.setattr(file_client, '_MAX_UPLOAD_SIZE', 5)
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'exit_code': 0}
    try:
        assert (await router.upload_container_file(user_id='alice', path='ok', content=b'12345'))['size'] == 5
        control.invoke.reset_mock()
        with pytest.raises(AgentOSFileTransferError) as caught:
            await router.upload_container_file(user_id='alice', path='too-large', content=b'123456')
        assert caught.value.code == 'file_too_large'
        assert not any(call.args[1] == 'file.rename' for call in control.invoke.await_args_list)
        assert any(call.args[1] == 'file.remove' for call in control.invoke.await_args_list)
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_upload_mode_is_validated_and_applied_before_publish(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'exit_code': 0}
    try:
        with pytest.raises(AgentOSFileTransferError) as caught:
            await router.upload_container_file(user_id='alice', path='x', content=b'x', mode='999')
        assert caught.value.code == 'BAD_REQUEST'
        assert router.test_api.creates == []
        await router.upload_container_file(user_id='alice', path='x', content=b'x', mode='0600')
        calls = control.invoke.await_args_list
        chmod = next(i for i, call in enumerate(calls) if call.args[1] == 'cmd.run' and 'chmod' in call.args[2]['cmd'])
        rename = next(i for i, call in enumerate(calls) if call.args[1] == 'file.rename')
        assert chmod < rename
        assert 'chmod -- 0600 ' in calls[chmod].args[2]['cmd']
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_invalid_directory_mode_is_rejected_before_activation(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    try:
        with pytest.raises(AgentOSFileTransferError) as caught:
            await router.mkdir_container_dir(user_id='alice', path='/home/agentos/a', mode='999')
        assert caught.value.code == 'BAD_REQUEST'
        assert router.test_api.creates == []
        control.invoke.assert_not_awaited()
    finally:
        await router.shutdown()


@pytest.mark.parametrize('status, payload, expected', [
    (400, {'error': 'path not found: /home/agentos/a'}, 'file_not_found'),
    (404, {'error': {'code': 'NOT_FOUND', 'message': 'instance not found'}}, 'instance_not_found'),
    (404, {'error': 'not found'}, 'INTERNAL_ERROR'),
    (404, {'error': 'missing', 'error_code': 'FILE_NOT_FOUND'}, 'file_not_found'),
])
@pytest.mark.asyncio
async def test_invoke_distinguishes_structured_instance_and_file_errors(monkeypatch, status, payload, expected):
    control = AdxControlClient(_config(monkeypatch), transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)))
    try:
        with pytest.raises(AdxControlError) as caught:
            await control.invoke('test-alice', 'file.stat', {'path': '/home/agentos/a'})
        assert caught.value.code == expected
    finally:
        await control.close()


@pytest.mark.asyncio
async def test_multipart_upload_forwards_and_rejects_invalid_mode(monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app

    router, control = _files(monkeypatch)
    await router.connect('')
    control.invoke.return_value = {'exit_code': 0}
    channel = _file_channel(router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_web_channel_app(channel)),
                                     base_url='http://gateway') as client:
            invalid = await client.post('/file-api/upload', data={'user_id': 'alice', 'mode': '999'},
                                        files={'file': ('a.txt', b'a')})
            assert invalid.json()['errors'] and not invalid.json()['files']
            assert not router.test_api.creates
            valid = await client.post('/file-api/upload', data={'user_id': 'alice', 'mode': '600'},
                                      files={'file': ('a.txt', b'a')})
        assert valid.json()['files'] and not valid.json()['errors']
        assert any(call.args[1] == 'cmd.run' and 'chmod -- 600 ' in call.args[2]['cmd']
                   for call in control.invoke.await_args_list)
    finally:
        await router.shutdown()


@pytest.mark.asyncio
async def test_upload_does_not_publish_when_mode_cannot_be_applied(monkeypatch):
    router, control = _files(monkeypatch)
    await router.connect('')
    def invoke(env, action, args):
        if action == 'cmd.run' and 'chmod' in args['cmd']:
            return {'exit_code': 1}
        return {'exit_code': 0}
    control.invoke.side_effect = invoke
    try:
        with pytest.raises(AgentOSFileTransferError):
            await router.upload_container_file(user_id='alice', path='a', content=b'a', mode='600')
        assert not any(call.args[1] == 'file.rename' for call in control.invoke.await_args_list)
        assert any(call.args[1] == 'file.remove' for call in control.invoke.await_args_list)
    finally:
        await router.shutdown()
