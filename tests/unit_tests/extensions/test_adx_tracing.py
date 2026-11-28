"""Business traces survive ADX requests without changing replay identities."""
import json
from uuid import UUID

import httpx
import pytest

from jiuwenswarm.extensions.agentos.agentos_router.adx.control_client import AdxControlClient, AdxCreateUncertain
from jiuwenswarm.extensions.agentos.agentos_router.adx.file_transport import file_request
from jiuwenswarm.gateway import telemetry
from test_adx_environment import config, envelope


@pytest.mark.asyncio
async def test_create_retries_keep_operation_identity_and_return_business_trace(monkeypatch, tmp_path):
    calls = []
    posts = []
    exists = False

    def handle(request):
        nonlocal exists
        calls.append(request)
        if request.method == 'GET':
            rows = [{'id': 'test-alice', 'tenant_id': 'tenant', 'status': 'running'}]
            return httpx.Response(200, json=rows if exists else [])
        if request.method == 'POST':
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(503, json={'error': {'code': 'SERVICE_UNAVAILABLE'}})
            exists = True
            return httpx.Response(200, json=envelope({'environmentId': 'test-alice'}))
        return httpx.Response(200, json={'success': True})

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    args = dict(namespace='test', name='alice', workspace='/mnt/alice',
                runtime_spec={'rootfs': {'imageurl': 'agent:1'}, 'cmds': [['/bin/app']]})
    try:
        with telemetry.bind_request_trace(None):
            with pytest.raises(AdxCreateUncertain):
                await client.create_sandbox(**args, trace_id='first-business-trace')
            result = await client.create_sandbox(**args, trace_id='second-business-trace')
            assert result.metadata['trace_id'] == 'second-business-trace'
            await client.wait_until_running('test-alice', trace_id='query-business-trace')
            await client.delete_sandbox('test-alice', trace_id='delete-business-trace')
        assert posts[0].headers['x-request-id'] == posts[1].headers['x-request-id']
        assert posts[0].headers['x-request-id'].startswith('create-')
        assert posts[0].headers['x-trace-id'] == 'first-business-trace'
        assert posts[1].headers['x-trace-id'] == 'second-business-trace'
        assert json.loads(posts[0].content) == json.loads(posts[1].content)
        assert calls[-1].headers['x-trace-id'] == 'delete-business-trace'
        assert calls[-1].headers['x-request-id'].startswith('delete-')
        assert any(r.headers['x-trace-id'] == 'query-business-trace' for r in calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_w3c_context_reaches_control_invoke_and_native_files(monkeypatch, tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b'payload') if request.url.path.endswith('/download') else httpx.Response(200, json={})

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('request', 'file.mkdir', channel='web', connection_id='connection')
    try:
        with telemetry.bind_request_trace(operation):
            await client._request('GET', '/api/instances')
            await client.invoke('test-alice', 'file.exists', {'path': '/home/agentos'})
            async with file_request(client, 'test-alice', 'GET', '/home/agentos/file') as response:
                assert await response.aread() == b'payload'
        for request in calls:
            assert request.headers['x-trace-id'] == str(UUID(hex=operation.trace_id))
            assert request.headers['traceparent'].split('-')[1] == operation.trace_id
            assert request.headers['authorization'] == 'Bearer platform-secret'
        assert calls[1].headers['x-adx-request-id'].startswith('invoke-')
        assert json.loads(calls[1].content)['requestId'] == calls[1].headers['x-adx-request-id']
    finally:
        operation.finish('success')
        telemetry.configure_gateway_telemetry({'telemetry': {'enabled': False}})
        await client.close()


@pytest.mark.asyncio
async def test_explicit_trace_id_wins_over_bound_context(monkeypatch, tmp_path):
    """The caller create trace must replace the warmup span, not the reverse."""
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    monkeypatch.setattr(telemetry, '_runtime', None)
    telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace(
        'request', 'file.mkdir', channel='web', connection_id='connection',
    )
    explicit = '6a945399-1111-4111-8111-111111111111'
    try:
        with telemetry.bind_request_trace(operation):
            await client._request('GET', '/api/instances', trace_id=explicit)
        request = calls[0]
        assert request.headers['x-trace-id'] == explicit
        assert request.headers['traceparent'].split('-')[1] == explicit.replace('-', '')
    finally:
        operation.finish('success')
        await client.close()


@pytest.mark.asyncio
async def test_builtin_create_injects_gateway_traceparent(monkeypatch, tmp_path):
    posts = []

    def handle(request):
        if request.method == 'GET':
            rows = [{'id': 'test-alice', 'tenant_id': 'tenant', 'status': 'running'}] if posts else []
            return httpx.Response(200, json=rows)
        posts.append(request)
        return httpx.Response(200, json=envelope({'environmentId': 'test-alice'}))

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('request', 'gw.agent.create', channel='web', connection_id='connection')
    try:
        with telemetry.bind_request_trace(operation):
            await client.create_sandbox(
                namespace='test', name='alice', workspace='/mnt/alice',
                runtime_spec={}, env_vars={}, builtin=True,
            )
        assert len(posts) == 1
        env = json.loads(posts[0].content)['env']
        traceparent = env['TRACEPARENT'].split('-')
        assert traceparent[1] == operation.trace_id
        assert traceparent[2] == format(operation.span.get_span_context().span_id, '016x')
    finally:
        operation.finish('success')
        telemetry.configure_gateway_telemetry({'telemetry': {'enabled': False}})
        await client.close()


@pytest.mark.asyncio
async def test_custom_agent_create_keeps_no_gateway_traceparent(monkeypatch, tmp_path):
    posts = []

    def handle(request):
        if request.method == 'GET':
            rows = [{'id': 'test-third', 'tenant_id': 'tenant', 'status': 'running'}] if posts else []
            return httpx.Response(200, json=rows)
        posts.append(request)
        return httpx.Response(200, json=envelope({'environmentId': 'test-third'}))

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('request', 'gw.agent.create', channel='web', connection_id='connection')
    try:
        with telemetry.bind_request_trace(operation):
            await client.create_sandbox(
                namespace='test', name='third', workspace='/mnt/third',
                runtime_spec={'rootfs': {'imageurl': 'third:1'}}, env_vars={}, builtin=False,
            )
        assert len(posts) == 1
        assert 'TRACEPARENT' not in json.loads(posts[0].content)['env']
    finally:
        operation.finish('success')
        telemetry.configure_gateway_telemetry({'telemetry': {'enabled': False}})
        await client.close()


@pytest.mark.asyncio
async def test_create_replay_tolerates_changed_trace_context(monkeypatch, tmp_path):
    posts = []
    exists = False

    def handle(request):
        nonlocal exists
        if request.method == 'GET':
            rows = [{'id': 'test-alice', 'tenant_id': 'tenant', 'status': 'running'}]
            return httpx.Response(200, json=rows if exists else [])
        posts.append(request)
        if len(posts) == 1:
            return httpx.Response(503, json={'error': {'code': 'SERVICE_UNAVAILABLE'}})
        exists = True
        return httpx.Response(200, json=envelope({'environmentId': 'test-alice'}))

    client = AdxControlClient(config(monkeypatch, tmp_path), transport=httpx.MockTransport(handle))
    telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    first = telemetry.start_request_trace('first', 'gw.agent.create', channel='web', connection_id='connection')
    second = telemetry.start_request_trace('second', 'gw.agent.create', channel='web', connection_id='connection')
    args = dict(namespace='test', name='alice', workspace='/mnt/alice',
                runtime_spec={}, env_vars={}, builtin=True)
    try:
        with telemetry.bind_request_trace(first):
            with pytest.raises(AdxCreateUncertain):
                await client.create_sandbox(**args)
        with telemetry.bind_request_trace(second):
            result = await client.create_sandbox(**args)
        assert result.sandbox_id == 'test-alice'
        bodies = [json.loads(post.content) for post in posts]
        assert bodies[0]['env']['TRACEPARENT'].split('-')[1] == first.trace_id
        assert bodies[1]['env']['TRACEPARENT'].split('-')[1] == second.trace_id
        assert first.trace_id != second.trace_id
    finally:
        first.finish('success')
        second.finish('success')
        telemetry.configure_gateway_telemetry({'telemetry': {'enabled': False}})
        await client.close()
