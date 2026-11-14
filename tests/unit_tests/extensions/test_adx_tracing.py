"""Business traces survive ADX requests without changing replay identities."""
import json
from uuid import UUID

import httpx
import pytest

from jiuwenswarm.extensions.adx.control_client import AdxControlClient, AdxCreateUncertain
from jiuwenswarm.extensions.adx.file_transport import file_request
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
            await client._request('GET', '/api/instances', trace_id='older-trace')
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
