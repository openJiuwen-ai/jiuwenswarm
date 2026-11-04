# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_gateway import telemetry
from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


@pytest.fixture
def setup_trace(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=telemetry.create_request_id_generator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, '_runtime', telemetry.GatewayTelemetry(tracer=provider.get_tracer('test')))
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda message: False)
    yield channel, exporter
    provider.shutdown()


def request(method='models.list', params=None):
    return json.dumps({'type': 'req', 'id': 'same', 'method': method, 'params': params or {},
                       'traceparent': '00-11111111111111111111111111111111-1111111111111111-01'})


@pytest.mark.asyncio
async def test_distinct_roots_and_scope_restored(setup_trace):
    channel, exporter = setup_trace
    seen = []
    async def handler(ws, req_id, params, session_id):
        seen.append(telemetry.current_request_trace())
    channel.register_method('models.list', handler, local_only=True)
    for ws in (SimpleNamespace(), SimpleNamespace()):
        await channel._handle_raw_message(ws, request(), {})
        assert telemetry.current_request_trace() is None
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].context.trace_id != spans[1].context.trace_id
    assert all(span.parent is None for span in spans)
    assert all(span.attributes['gateway.outcome'] == 'incomplete' for span in spans)
    assert len({op.connection_id for op in seen}) == 2
    assert not channel._request_traces


@pytest.mark.asyncio
async def test_message_context_is_isolated_on_same_connection(setup_trace):
    channel, exporter = setup_trace
    async def handler(ws, req_id, params, session_id):
        pass
    channel.register_method('models.list', handler, local_only=True)
    ws = SimpleNamespace()
    contexts = [
        {'traceparent': '00-11111111111111111111111111111111-2222222222222222-01'},
        {'trace_id': '33333333333333333333333333333333'},
        {'traceparent': 'invalid'},
        None,
    ]
    for context in contexts:
        message = json.loads(request())
        message['trace_context'] = context
        await channel._handle_raw_message(ws, json.dumps(message), {})
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    assert spans[0].context.trace_id == int('1' * 32, 16)
    assert spans[0].parent.span_id == int('2' * 16, 16)
    assert spans[1].context.trace_id == int('3' * 32, 16)
    assert spans[1].parent is None
    assert all(span.parent is None for span in spans[2:])
    assert len({span.context.trace_id for span in spans}) == 4
    assert not channel._request_traces


@pytest.mark.asyncio
async def test_unsupported_method_clears_inherited_scope(setup_trace):
    channel, exporter = setup_trace
    observed = []
    async def handler(ws, req_id, params, session_id):
        observed.append(telemetry.current_request_trace())
    channel.register_method('config.get', handler, local_only=True)
    parent = telemetry.start_request_trace('outer', 'models.list', channel=channel.channel_id, connection_id='outer')
    with telemetry.bind_request_trace(parent):
        await channel._handle_raw_message(SimpleNamespace(), request('config.get'), {})
        assert telemetry.current_request_trace() is parent
    assert observed == [None]
    assert not exporter.get_finished_spans()
    parent.finish('incomplete')


@pytest.mark.asyncio
async def test_deferred_trace_starter_respects_capacity(setup_trace, monkeypatch):
    channel, exporter = setup_trace
    attempts = []
    monkeypatch.setattr(channel, '_track_request_trace', lambda ws, op: attempts.append(op) or False)

    async def handler(ws, req_id, params, session_id):
        assert telemetry.ensure_request_trace(req_id, channel.channel_id) is None
        assert telemetry.ensure_request_trace(req_id, channel.channel_id) is None

    channel.register_method('config.get', handler, local_only=True)
    await channel._handle_raw_message(SimpleNamespace(), request('config.get'), {})
    assert len(attempts) == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes['gateway.reason'] == 'capacity'
    assert not channel._request_traces


@pytest.mark.asyncio
async def test_deferred_trace_starter_expires_with_request(setup_trace):
    channel, exporter = setup_trace
    release = asyncio.Event()
    tasks = []

    async def handler(ws, req_id, params, session_id):
        async def later():
            await release.wait()
            return telemetry.ensure_request_trace(req_id, channel.channel_id)
        tasks.append(asyncio.create_task(later()))

    channel.register_method('config.get', handler, local_only=True)
    await channel._handle_raw_message(SimpleNamespace(), request('config.get'), {})
    release.set()
    assert await asyncio.wait_for(tasks[0], 2) is None
    assert not exporter.get_finished_spans()
    assert not channel._request_traces
    assert telemetry.current_request_trace() is None


@pytest.mark.asyncio
async def test_mode_rejection_and_file_failure_finish_trace(setup_trace):
    channel, exporter = setup_trace
    channel.send_response = AsyncMock()
    await channel._handle_raw_message(SimpleNamespace(), request(params={'mode': []}), {})
    channel.send_response.assert_awaited_once()
    channel._process_files = AsyncMock(side_effect=RuntimeError('failure'))
    with pytest.raises(RuntimeError):
        await channel._handle_raw_message(SimpleNamespace(), request('chat.send'), {})
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(s.attributes['gateway.outcome'] == 'incomplete' for s in spans)
    assert not channel._request_traces
    assert telemetry.current_request_trace() is None


@pytest.mark.asyncio
async def test_cancellation_finishes_and_restores_scope(setup_trace):
    channel, exporter = setup_trace
    channel._process_files = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await channel._handle_raw_message(SimpleNamespace(), request('chat.send'), {})
    assert len(exporter.get_finished_spans()) == 1
    assert not channel._request_traces
    assert telemetry.current_request_trace() is None


class Operation(telemetry.TraceOperation):
    def __init__(self, ws):
        provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
        span = provider.get_tracer('test').start_span('request')
        super().__init__('same', 'models.list', 'web', str(id(ws)), span,
                         trace.set_span_in_context(span, context.Context()), 0)

    @property
    def events(self):
        return [(event.name, dict(event.attributes)) for event in self.span.events]

    @property
    def result(self):
        return (self.outcome, self.reason) if self.finished else None


class Socket:
    def __init__(self, ws_id):
        self._jiuwen_ws_id = ws_id
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent = []
        self.fail = False

    async def send(self, wire):
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError('secret')
        self.sent.append(wire)


def setup_channel(ws):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._send_queues[ws._jiuwen_ws_id] = asyncio.Queue(maxsize=2)
    return channel


def enqueue(channel, ws, op, frame, monkeypatch):
    monkeypatch.setattr(telemetry, 'current_request_trace', lambda: op)
    channel._enqueue_send(ws, frame)
    if op.connection_id == str(id(ws)) and not frame.get('payload', {}).get('accepted'):
        op.complete()


def response(**kwargs):
    return {'type': 'res', 'id': 'same', 'ok': True, 'payload': kwargs}


@pytest.mark.asyncio
@pytest.mark.parametrize('event_limit_reached', [False, True])
async def test_send_completion_and_wire_contents(monkeypatch, event_limit_reached):
    ws = Socket('one')
    channel = setup_channel(ws)
    op = Operation(ws)
    if event_limit_reached:
        for _ in range(100):
            op.event('preprocess.credentials', {'duration_ms': 1})
    assert channel._track_request_trace(ws, op)
    channel._writers['one'] = asyncio.create_task(channel._writer_loop(ws, 'one'))
    enqueue(channel, ws, op, response(models=[]), monkeypatch)
    await asyncio.wait_for(ws.started.wait(), 1)
    assert op.delivery_pending and not op.finished
    ws.release.set()
    await asyncio.wait_for(channel._drain_and_cleanup_writer(ws, 'one'), 1)
    assert op.result == ('success', '')
    written = next(attributes for name, attributes in op.events if name == 'client.written')
    assert written['outcome'] == 'written'
    assert {'queue_wait_ms', 'send_ms'} <= set(written)
    assert json.loads(ws.sent[0]) == response(models=[])
    assert not channel._request_traces


@pytest.mark.asyncio
async def test_send_failure_and_pending_request_cleanup(monkeypatch):
    ws = Socket('one')
    ws.fail = True
    ws.release.set()
    channel = setup_channel(ws)
    op, waiting = Operation(ws), Operation(ws)
    channel._track_request_trace(ws, op)
    channel._track_request_trace(ws, waiting)
    channel._writers['one'] = asyncio.create_task(channel._writer_loop(ws, 'one'))
    enqueue(channel, ws, op, response(), monkeypatch)
    await asyncio.wait_for(channel._writers['one'], 1)
    assert op.result == ('incomplete', 'send_failed')
    assert waiting.result == ('incomplete', 'writer_stopped')
    assert not channel._request_traces


def test_queue_full_and_closed_drop(monkeypatch):
    ws = Socket('one')
    channel = setup_channel(ws)
    q = channel._send_queues['one']
    q.put_nowait('first')
    q.put_nowait('second')
    op = Operation(ws)
    channel._track_request_trace(ws, op)
    enqueue(channel, ws, op, response(), monkeypatch)
    assert op.result == ('incomplete', 'queue_full')
    assert [q.get_nowait(), q.get_nowait()] == ['first', 'second']
    ws.closed = True
    op = Operation(ws)
    channel._track_request_trace(ws, op)
    enqueue(channel, ws, op, response(), monkeypatch)
    assert op.result == ('incomplete', 'writer_unavailable')


@pytest.mark.asyncio
async def test_same_id_on_different_sockets_and_accepted(monkeypatch):
    ws, other = Socket('one'), Socket('two')
    channel = setup_channel(ws)
    channel._send_queues['two'] = asyncio.Queue()
    op, other_op = Operation(ws), Operation(other)
    channel._track_request_trace(ws, op)
    channel._track_request_trace(other, other_op)
    enqueue(channel, other, op, response(), monkeypatch)
    assert not op.delivery_pending and not other_op.delivery_pending
    enqueue(channel, ws, op, response(accepted=True), monkeypatch)
    assert op.delivery_pending and not op.finished
    enqueue(channel, ws, op, response(), monkeypatch)
    ws.release.set()
    channel._writers['one'] = asyncio.create_task(channel._writer_loop(ws, 'one'))
    await asyncio.wait_for(channel._drain_and_cleanup_writer(ws, 'one'), 1)
    assert len(ws.sent) == 2 and op.result == ('success', '')
    assert not other_op.finished
    channel._finish_ws_traces(other, 'connection_closed')


@pytest.mark.asyncio
async def test_stream_delivery_coalesces_without_logging_every_chunk(monkeypatch):
    ws = Socket('stream')
    channel = setup_channel(ws)
    channel._send_queues['stream'] = asyncio.Queue(maxsize=512)
    op = Operation(ws)
    op.method = 'history.get'
    op.accepted = True
    channel._track_request_trace(ws, op)
    summaries = []
    monkeypatch.setattr(telemetry.logger, 'info', lambda *args: summaries.append(args))
    with telemetry.bind_request_trace(op):
        for _ in range(100):
            channel._enqueue_send(ws, {
                'type': 'event', 'event': 'chat.delta',
                'payload': {'request_id': 'same', 'session_id': 'session', 'content': 'x'},
            })
    op.complete()
    assert not op.finished and op.pending_deliveries == 100
    ws.release.set()
    channel._writers['stream'] = asyncio.create_task(channel._writer_loop(ws, 'stream'))
    await asyncio.wait_for(channel._drain_and_cleanup_writer(ws, 'stream'), 2)
    assert op.finished and op.outcome == 'success' and op.pending_deliveries == 0
    assert ''.join(json.loads(frame)['payload']['content'] for frame in ws.sent) == 'x' * 100
    assert len(ws.sent) == 4
    assert len(summaries) == 1 and summaries[0][1] == 'client.written'
    assert len([name for name, _ in op.events if name == 'client.first_content']) == 1
