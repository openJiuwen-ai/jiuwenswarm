# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import threading
import uuid

import pytest
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from e2a.models import E2AEnvelope
from e2a.agent_models import AgentResponse, AgentResponseChunk
from e2a.wire_codec import encode_agent_response_for_wire, encode_agent_chunk_for_wire
from agent_gateway import telemetry
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from agent_gateway.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('derived', [False, True])
async def test_wire_context_and_scope_cleanup(monkeypatch, stream, derived):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    parent = telemetry.start_request_trace('parent', 'chat.send', channel='web', connection_id='c')
    original_traceparent = '00-11111111111111111111111111111111-2222222222222222-01'
    channel_context = {'business': 'value', 'traceparent': 'business-trace', 'tracestate': 'business-state'}
    envelope = E2AEnvelope(request_id='child' if derived else 'parent', method='chat.send', channel='web',
                           channel_context=channel_context.copy(), params={'message': 'hello'},
                           traceparent=original_traceparent)
    client = WebSocketAgentServerClient()
    client._server_ready = True
    captured = {}

    class Socket:
        closed = False
        async def send(self, raw):
            captured['wire'] = json.loads(raw)
            captured['operation'] = telemetry.current_request_trace()
            if stream:
                response = AgentResponseChunk(request_id=envelope.request_id, channel_id='web', payload={'content': 'x'})
                wire = encode_agent_chunk_for_wire(response, response_id=envelope.request_id, sequence=0)
            else:
                response = AgentResponse(request_id=envelope.request_id, channel_id='web', ok=True, payload={})
                wire = encode_agent_response_for_wire(response, response_id=envelope.request_id)
            client._message_queues[envelope.request_id].put_nowait(wire)

    client._ws = Socket()
    with telemetry.bind_request_trace(parent):
        if stream:
            generator = client.send_request_stream(envelope)
            await anext(generator)
            assert telemetry.current_request_trace() is parent
            await generator.aclose()
        else:
            assert (await client.send_request(envelope)).ok
        assert telemetry.current_request_trace() is parent
    operation = captured['operation']
    wire = captured['wire']
    decoded = E2AEnvelope.from_dict(wire)
    assert decoded.traceparent == wire['traceparent']
    remote = trace.get_current_span(TraceContextTextMapPropagator().extract(
        {'traceparent': wire['traceparent']})).get_span_context()
    assert remote.trace_id == parent.span.get_span_context().trace_id
    assert remote.span_id == operation.span.get_span_context().span_id
    assert (operation is not parent) == derived
    if derived:
        assert operation.span.parent.span_id == parent.span.get_span_context().span_id
        assert operation.finished
    assert envelope.traceparent == original_traceparent
    assert envelope.channel_context == channel_context
    assert wire['channel_context'] == channel_context
    assert decoded.channel_context == channel_context
    assert wire['params'] == {'message': 'hello'}
    assert not client._message_queues
    parent.finish('success')
    runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_send_failure_is_not_a_successful_send(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    client = WebSocketAgentServerClient()
    client._server_ready = True
    class Socket:
        closed = False
        async def send(self, raw):
            raise ValueError('broken')
    client._ws = Socket()
    with telemetry.bind_request_trace(operation):
        with pytest.raises(ValueError, match='broken'):
            await client.send_request(E2AEnvelope(request_id='r', method='chat.send', channel='web'))
    names = [event.name for event in operation.span.events]
    assert 'send.failed' in names and 'send.returned' not in names
    assert not client._message_queues
    operation.finish('failed')
    runtime.provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('traceparent', [None, '00-11111111111111111111111111111111-2222222222222222-01'])
async def test_disabled_transport_preserves_envelope_and_cleanup(monkeypatch, stream, traceparent):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    client = WebSocketAgentServerClient()
    client._server_ready = True
    captured = {}
    class Socket:
        closed = False
        async def send(self, raw):
            captured.update(json.loads(raw))
            if stream:
                response = AgentResponseChunk(request_id='r', channel_id='web', payload={'content': 'x'})
                wire = encode_agent_chunk_for_wire(response, response_id='r', sequence=0)
            else:
                response = AgentResponse(request_id='r', channel_id='web', ok=True, payload={})
                wire = encode_agent_response_for_wire(response, response_id='r')
            client._message_queues['r'].put_nowait(wire)
    client._ws = Socket()
    channel_context = {'business': 'value', 'traceparent': 'legacy', 'tracestate': 'business-state'}
    envelope = E2AEnvelope(request_id='r', method='chat.send', channel='web',
                           channel_context=channel_context.copy(), traceparent=traceparent)
    if stream:
        generator = client.send_request_stream(envelope)
        await anext(generator)
        await generator.aclose()
    else:
        assert (await client.send_request(envelope)).ok
    decoded = E2AEnvelope.from_dict(captured)
    assert captured['traceparent'] == traceparent
    assert decoded.traceparent == traceparent
    assert envelope.traceparent == traceparent
    assert captured['channel_context'] == channel_context
    assert decoded.channel_context == channel_context
    assert envelope.channel_context == channel_context
    assert not client._message_queues


@pytest.mark.asyncio
async def test_unary_timeout_cleans_queue_and_reports_timeout(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    client = WebSocketAgentServerClient()
    client._server_ready = True
    class Socket:
        closed = False
        async def send(self, raw):
            pass
    client._ws = Socket()
    with telemetry.bind_request_trace(operation):
        with pytest.raises(RuntimeError, match='超时'):
            await client.send_request(E2AEnvelope(request_id='r', method='chat.send', channel='web'), timeout=0.01)
        assert telemetry.current_request_trace() is operation
    assert operation.outcome == 'timeout'
    assert not client._message_queues
    operation.finish('timeout')
    runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_stream_producer_cancelled_error_reaches_consumer(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    client = WebSocketAgentServerClient()
    client._server_ready = True
    class Socket:
        closed = False
        async def send(self, raw):
            raise asyncio.CancelledError()
    client._ws = Socket()
    with telemetry.bind_request_trace(operation):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(anext(client.send_request_stream(
                E2AEnvelope(request_id='r', method='chat.send', channel='web'))), 1)
        assert telemetry.current_request_trace() is operation
    assert not client._message_queues
    operation.finish('cancelled')
    runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_stream_close_cancels_blocked_producer_and_ends_child(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    parent = telemetry.start_request_trace('parent', 'chat.send', channel='web', connection_id='c')
    client = WebSocketAgentServerClient()
    client._server_ready = True
    observed = {}
    class Socket:
        closed = False
        async def send(self, raw):
            observed['child'] = telemetry.current_request_trace()
            for sequence in range(5):
                chunk = AgentResponseChunk(request_id='child', channel_id='web', payload={'sequence': sequence})
                client._message_queues['child'].put_nowait(
                    encode_agent_chunk_for_wire(chunk, response_id='child', sequence=sequence))
    client._ws = Socket()
    envelope = E2AEnvelope(request_id='child', method='chat.send', channel='web')
    with telemetry.bind_request_trace(parent):
        stream = client.send_request_stream(envelope)
        await anext(stream)
        await asyncio.sleep(0)
        await asyncio.wait_for(stream.aclose(), 1)
        assert telemetry.current_request_trace() is parent
    assert observed['child'].finished
    assert not client._message_queues
    parent.finish('success')
    runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_traced_stream_waits_for_consumer_before_advancing(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    client = WebSocketAgentServerClient()
    advanced = []
    async def source(_envelope):
        for value in range(3):
            advanced.append(value)
            yield value
    monkeypatch.setattr(client, '_send_request_stream_scoped', source)
    with telemetry.bind_request_trace(operation):
        stream = client.send_request_stream(E2AEnvelope(request_id='r', method='chat.send', channel='web'))
        assert await anext(stream) == 0
        await asyncio.sleep(0)
        assert advanced == [0]
        assert await anext(stream) == 1
        await stream.aclose()
    operation.finish('success')
    runtime.provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
async def test_invoke_headers_in_worker_thread(monkeypatch, stream, enabled):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': enabled, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c',
        trace_context={'traceparent': '00-11111111111111111111111111111111-2222222222222222-01',
                       'tracestate': 'vendor=value'})
    main_thread = threading.get_ident()
    observed = {}
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, *args):
            if observed.get('read'):
                return b''
            observed['read'] = True
            if stream:
                return b'data: {"content":"hello"}\n\n'
            return b'{"content":"hello"}'
    def urlopen(request, timeout):
        observed['headers'] = {k.lower(): v for k, v in request.header_items()}
        observed['thread'] = threading.get_ident()
        observed['operation'] = telemetry.current_request_trace()
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    envelope = E2AEnvelope(request_id='r', method='chat.send', channel='web', session_id='s',
                           user_id='u', params={'message': 'hello'})
    with telemetry.bind_request_trace(operation):
        if stream:
            result = [chunk async for chunk in client.send_request_stream(envelope)]
            assert result
        else:
            await client.send_request(envelope)
        assert telemetry.current_request_trace() is operation
    headers = observed['headers']
    assert observed['thread'] != main_thread
    assert 'x-instance-session' in headers and 'x-session-context' in headers
    if enabled:
        remote = trace.get_current_span(TraceContextTextMapPropagator().extract(headers)).get_span_context()
        assert remote.trace_id == operation.span.get_span_context().trace_id
        assert remote.span_id == operation.span.get_span_context().span_id
        assert headers['tracestate'] == 'vendor=value'
        assert uuid.UUID(headers['x-trace-id']).int == remote.trace_id
        assert observed['operation'] is operation
        assert any(event.name == 'http.response_headers' for event in operation.span.events)
        assert not any(event.name == 'send.returned' for event in operation.span.events)
        operation.finish('success')
        runtime.provider.shutdown()
    else:
        assert 'traceparent' not in headers
        assert observed['operation'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_invoke_transport_failure_restores_context(monkeypatch, stream):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('parent', 'chat.send', channel='web', connection_id='c')
    observed = {}
    def fail(request, timeout):
        observed['child'] = telemetry.current_request_trace()
        raise OSError('connection failed')
    monkeypatch.setattr('urllib.request.urlopen', fail)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    envelope = E2AEnvelope(request_id='child', method='chat.send', channel='web', session_id='s')
    with telemetry.bind_request_trace(operation):
        if stream:
            with pytest.raises(RuntimeError, match='connection failed'):
                async for _ in client.send_request_stream(envelope):
                    pass
        else:
            response = await client.send_request(envelope)
            assert not response.ok
        assert telemetry.current_request_trace() is operation
    assert observed['child'].finished
    assert observed['child'].outcome == 'failed'
    assert not any(event.name in {'http.response_headers', 'response.first'}
                   for event in observed['child'].span.events)
    operation.finish('success')
    runtime.provider.shutdown()




@pytest.mark.asyncio
async def test_unary_api_error_returns_expected_response(monkeypatch):
    from agent_gateway.extensions.yuanrong_frontend_client import YuanrongAgentApiError

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    def fail(*args):
        raise YuanrongAgentApiError('controlled failure')
    monkeypatch.setattr(client, '_do_invoke', fail)
    try:
        with telemetry.bind_request_trace(operation):
            response = await client.send_request(E2AEnvelope(
                request_id='r', channel='web', method='chat.send', session_id='s',
            ))
        assert response.request_id == 'r'
        assert response.channel_id == 'web'
        assert response.ok is False
        assert response.payload == {'error': 'controlled failure'}
        assert operation.outcome == 'failed'
    finally:
        operation.finish('failed')
        runtime.provider.shutdown()
