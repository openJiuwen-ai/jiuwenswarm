# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from e2a.agent_models import AgentResponse, AgentResponseChunk
from e2a.models import E2AEnvelope
from e2a.wire_codec import encode_agent_response_for_wire, encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.req_method import ReqMethod
from agent_gateway import app_gateway, telemetry
from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.channel_manager import ChannelManager
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from agent_gateway.message_handler.message_handler import MessageHandler
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from agent_gateway.routing.e2a_proxy import proxy_unary_request
from agent_gateway.routing.keys import AgentRef, RoutingKey
from agent_gateway.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient

from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import mask_sensitive


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path):
    from jiuwenswarm.server.runtime.session import session_metadata

    flush_pending_writes = session_metadata.flush_pending_writes
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(utils, "_workspace_base_dir", tmp_path / ".jiuwenswarm")
        patch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / ".jiuwenswarm"))
        yield
        flush_pending_writes()


pytestmark = pytest.mark.integration


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 5)


@pytest.fixture
def collector():
    requests = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, ExportTraceServiceRequest.FromString(payload)))
            body = ExportTraceServiceResponse().SerializeToString()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def spans(requests):
    return [span for _, request in requests for resource in request.resource_spans
            for scope in resource.scope_spans for span in scope.spans]


class ClientSocket:
    closed = False
    remote_address = ("127.0.0.1", 12345)

    def __init__(self, block=False):
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sent = []
        self.fail_send = False

    async def send(self, value):
        frame = json.loads(value)
        self.entered.set()
        if self.fail_send:
            raise OSError("controlled client write failure")
        if self.block:
            await self.release.wait()
        self.sent.append(frame)


class AgentSocket:
    closed = False

    def __init__(self, client, runtime):
        self.client = client
        self.runtime = runtime
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.operation = None
        self.envelope = None
        self.wire = None
        self.fail_send = False

    async def send(self, value):
        self.operation = telemetry.current_request_trace()
        self.wire = json.loads(value)
        self.envelope = E2AEnvelope.from_dict(self.wire)
        self.entered.set()
        await self.release.wait()
        if self.fail_send:
            raise OSError("controlled agent send failure")
        if self.runtime.provider is not None:
            parent = TraceContextTextMapPropagator().extract({"traceparent": self.envelope.traceparent})
            with self.runtime.provider.get_tracer("controlled-agent").start_as_current_span(
                "controlled.agent", context=parent,
            ):
                pass

    def chunk(self, payload, *, complete=False, sequence=0):
        rid = self.envelope.request_id
        self.client._message_queues[rid].put_nowait(encode_agent_chunk_for_wire(
            AgentResponseChunk(request_id=rid, channel_id="web", payload=payload, is_complete=complete),
            response_id=rid, sequence=sequence,
        ))


class UnaryAgentSocket:
    closed = False

    def __init__(self, client, runtime, response_ok=True):
        self.client = client
        self.runtime = runtime
        self.response_ok = response_ok
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []
        self.operation = None

    async def send(self, value):
        self.operation = telemetry.current_request_trace()
        self.entered.set()
        await self.release.wait()
        envelope = E2AEnvelope.from_dict(json.loads(value))
        self.requests.append(envelope)
        parent = TraceContextTextMapPropagator().extract({"traceparent": envelope.traceparent})
        tracer = self.runtime.provider.get_tracer("controlled-agent")
        with tracer.start_as_current_span("controlled.agent", context=parent):
            response = AgentResponse(
                request_id=envelope.request_id, channel_id="web",
                ok=self.response_ok, payload={"models": []},
            )
            self.client._message_queues[envelope.request_id].put_nowait(
                encode_agent_response_for_wire(response, response_id=envelope.request_id),
            )


async def wait_finished(operation):
    while not operation.finished:
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_ok", [True, False])
@pytest.mark.parametrize("req_method", [
    ReqMethod.MODELS_LIST, ReqMethod.SESSION_LIST, ReqMethod.CONFIG_GET,
])
@pytest.mark.parametrize("incoming", [
    {"traceparent": "00-11111111111111111111111111111111-2222222222222222-01"},
    {"trace_id": "11111111-1111-1111-1111-111111111111"},
    None,
])
async def test_registered_unary_exports_after_actual_client_write(
    collector, monkeypatch, response_ok, req_method, incoming,
):
    endpoint, requests = collector
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.ProxyTracerProvider())
    runtime = telemetry.configure_gateway_telemetry(
        {"telemetry": {"enabled": True, "exporter": "otlp", "protocol": "http", "endpoint": endpoint}},
        user_id_formatter=lambda value: mask_sensitive(f"user_id={value}").removeprefix("user_id="),
    )
    client = WebSocketAgentServerClient()
    agent = UnaryAgentSocket(client, runtime, response_ok)
    client._ws = agent
    client._server_ready = True
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda message: False)
    socket = ClientSocket(block=True)
    channel._resolve_connection_user_id({"user_id": "alice"}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id="alice", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="session",
    ))

    async def proxy_request(ws, req_id, params, session_id, *, user_id=None):
        await proxy_unary_request(
            channel=channel, agent_client=client, ws=ws, req_id=req_id,
            params=params, session_id=session_id, user_id=user_id,
            req_method=req_method,
        )

    channel.register_method(req_method.value, proxy_request)
    task = asyncio.create_task(channel._handle_raw_message(socket, json.dumps({
        "type": "req", "id": "rpc-example", "method": req_method.value, "params": {},
        "trace_context": incoming,
    }), {}))
    try:
        await asyncio.wait_for(agent.entered.wait(), 5)
        operation = agent.operation
        assert operation is not None
        assert not operation.finished
        assert not any(event.name == "send.returned" for event in operation.span.events)

        agent.release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(socket.entered.wait(), 5)
        assert operation.delivery_pending and not operation.finished
        runtime.provider.force_flush(timeout_millis=5000)
        assert not any(span.name == "gw.session.message" for span in spans(requests))

        socket.release.set()
        await asyncio.wait_for(wait_finished(operation), 5)
        runtime.provider.force_flush(timeout_millis=5000)
        found = spans(requests)
        roots = [span for span in found if span.name == "gw.session.message"]
        assert len(roots) == 1
        root = roots[0]
        child = next(span for span in found if span.name == "controlled.agent")
        assert child.trace_id == root.trace_id
        assert child.parent_span_id == root.span_id
        assert root.trace_id.hex() == operation.trace_id
        if incoming is not None:
            assert root.trace_id.hex() == "1" * 32
        else:
            assert int(root.trace_id.hex(), 16) != 0
        if incoming is not None and "traceparent" in incoming:
            assert root.parent_span_id.hex() == "2" * 16
        else:
            assert root.parent_span_id == b""
        attributes = {item.key: item.value.string_value for item in root.attributes}
        assert attributes["request_id"] == "rpc-example"
        assert attributes["method"] == req_method.value
        assert attributes["gateway.outcome"] == ("success" if response_ok else "failed")
        assert "alice" not in attributes["user_id"]
        assert "fp:" in attributes["user_id"]
        assert {event.name for event in root.events} >= {
            "received", "send.returned", "response.received", "client.written",
        }
        assert all(path == "/v1/traces" for path, _ in requests)
        assert len(socket.sent) == 1
        assert socket.sent[0]["id"] == "rpc-example"
        assert socket.sent[0]["ok"] is response_ok
        if response_ok:
            assert socket.sent[0] == {
                "type": "res", "id": "rpc-example", "ok": True, "payload": {"models": []},
            }
        assert agent.requests[0].traceparent
        assert not {"traceparent", "tracestate"} & agent.requests[0].channel_context.keys()
        assert not channel._request_traces
        assert telemetry.current_request_trace() is None
    finally:
        agent.release.set()
        socket.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await channel._shutdown_all_writers()
        await telemetry.shutdown_gateway_telemetry()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['chat.send', 'history.get', 'command.goal'])
@pytest.mark.parametrize('incoming', [
    {'traceparent': '00-11111111111111111111111111111111-2222222222222222-01'},
    {'trace_id': '11111111-1111-1111-1111-111111111111'},
])
async def test_stream_trace_waits_for_producer_and_web_writer(collector, monkeypatch, method, incoming):
    endpoint, requests = collector
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.delenv('OTEL_TRACES_SAMPLER', raising=False)
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: trace.ProxyTracerProvider())
    monkeypatch.setattr(MessageHandler, '_instance', None)

    async def no_refresh(_msg):
        pass

    monkeypatch.setattr(app_gateway, '_refresh_expired_login_credential', no_refresh)

    async def no_hook(_self, _msg):
        pass

    monkeypatch.setattr(MessageHandler, '_trigger_before_chat_request_hook', no_hook)
    runtime = telemetry.configure_gateway_telemetry(
        {'telemetry': {'enabled': True, 'exporter': 'otlp', 'protocol': 'http', 'endpoint': endpoint}},
        user_id_formatter=lambda value: mask_sensitive(f'user_id={value}').removeprefix('user_id='),
    )
    client = WebSocketAgentServerClient()
    agent = AgentSocket(client, runtime)
    client._ws = agent
    client._server_ready = True
    handler = MessageHandler(client)
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    socket = ClientSocket()
    channel._resolve_connection_user_id({'user_id': 'alice'}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id='alice', channel_id='web', app_id='default',
        agent_ref=AgentRef(mode='agent', id='default'), session_id='session',
    ))

    async def forward(msg):
        await app_gateway._normalize_and_forward_message(msg, manager)
        return True

    async def ack(ws, req_id, params, session_id, *, user_id=None):
        await channel.send_response(ws, req_id, ok=True)

    channel.register_method('chat.send', ack)
    manager.register_channel_with_inbound(channel, forward)
    await handler.start_forwarding()
    await manager.start_dispatch()
    request = {
        'type': 'req', 'id': 'stream-example', 'method': method,
        'params': ({'session_id': 'session', 'action': 'set', 'objective': 'hello',
                    'overwrite_confirmed': True, 'mode': 'agent'}
                   if method == 'command.goal' else {'session_id': 'session', 'content': 'hello'}),
        'trace_context': incoming,
    }
    if method == 'command.goal':
        request['is_stream'] = True
    task = asyncio.create_task(channel._handle_raw_message(socket, json.dumps(request), {}))
    try:
        await asyncio.wait_for(agent.entered.wait(), 5)
        operation = agent.operation
        assert operation is not None and not operation.finished
        assert telemetry.current_request_trace() is None
        assert not any(event.name == 'send.returned' for event in operation.span.events)
        assert agent.envelope.traceparent
        assert agent.envelope.is_stream is True
        assert not {'traceparent', 'tracestate'} & agent.envelope.channel_context.keys()
        assert '_gateway_trace' not in json.dumps(agent.wire)
        if method == 'chat.send':
            await wait_for(lambda: any(frame.get('type') == 'res' for frame in socket.sent))
            assert not operation.finished
        agent.release.set()
        agent.chunk({'event_type': 'chat.final', 'content': 'phase complete'}, sequence=0)
        await wait_for(lambda: any(frame.get('event') == 'chat.final' for frame in socket.sent))
        assert not operation.finished
        runtime.provider.force_flush(timeout_millis=5000)
        assert not any(span.name == 'gw.session.message' for span in spans(requests))

        socket.block = True
        agent.chunk({'event_type': 'chat.delta', 'content': 'last'}, sequence=1)
        await asyncio.wait_for(socket.entered.wait(), 5)
        queue = channel._send_queues[socket._jiuwen_ws_id]
        agent.chunk({'event_type': 'chat.delta', 'content': 'one'}, sequence=2)
        agent.chunk({'event_type': 'chat.delta', 'content': 'two'}, sequence=3)
        await wait_for(lambda: queue.qsize() >= 2)
        agent.chunk({'is_complete': True}, complete=True, sequence=4)
        await wait_for(lambda: 'stream-example' not in handler._stream_tasks)
        assert not operation.finished and operation.pending_deliveries >= 3
        socket.release.set()
        await wait_for(lambda: operation.finished)
        assert operation.pending_deliveries == 0
        contents = [frame.get('payload', {}).get('content') for frame in socket.sent]
        assert 'onetwo' in contents
        runtime.provider.force_flush(timeout_millis=5000)
        roots = [span for span in spans(requests) if span.name == 'gw.session.message']
        assert len(roots) == 1
        root = roots[0]
        child = next(span for span in spans(requests) if span.name == 'controlled.agent')
        assert root.trace_id.hex() == '1' * 32
        assert root.parent_span_id.hex() == ('2' * 16 if 'traceparent' in incoming else '')
        assert child.trace_id == root.trace_id and child.parent_span_id == root.span_id
        assert {event.name for event in root.events} >= {'received', 'send.returned', 'response.received', 'client.written'}
        attributes = {item.key: item.value.string_value for item in root.attributes}
        assert attributes['gateway.outcome'] == 'success'
        assert attributes['method'] == method
        assert attributes['request_id'] == 'stream-example'
        assert 'alice' not in attributes['user_id']
        assert not channel._request_traces
        assert not client._message_queues
        assert telemetry.current_request_trace() is None
    finally:
        agent.release.set()
        socket.release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(manager.stop_dispatch(), 5)
        await asyncio.wait_for(handler.stop_forwarding(), 5)
        await asyncio.wait_for(channel._shutdown_all_writers(), 5)
        await asyncio.wait_for(telemetry.shutdown_gateway_telemetry(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['send', 'cancel', 'writer', 'queue_full'])
async def test_stream_trace_finishes_on_failure_or_cancellation(collector, monkeypatch, failure):
    endpoint, requests = collector
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.delenv('OTEL_TRACES_SAMPLER', raising=False)
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: trace.ProxyTracerProvider())
    monkeypatch.setattr(MessageHandler, '_instance', None)

    async def no_refresh(_msg):
        pass

    monkeypatch.setattr(app_gateway, '_refresh_expired_login_credential', no_refresh)

    async def no_hook(_self, _msg):
        pass

    monkeypatch.setattr(MessageHandler, '_trigger_before_chat_request_hook', no_hook)
    runtime = telemetry.configure_gateway_telemetry(
        {'telemetry': {'enabled': True, 'exporter': 'otlp', 'protocol': 'http', 'endpoint': endpoint}},
        user_id_formatter=lambda value: mask_sensitive(f'user_id={value}').removeprefix('user_id='),
    )
    client = WebSocketAgentServerClient()
    agent = AgentSocket(client, runtime)
    agent.fail_send = failure == 'send'
    client._ws = agent
    client._server_ready = True
    handler = MessageHandler(client)
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    socket = ClientSocket()
    channel._resolve_connection_user_id({'user_id': 'alice'}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id='alice', channel_id='web', app_id='default',
        agent_ref=AgentRef(mode='agent', id='default'), session_id='session',
    ))

    async def forward(msg):
        await app_gateway._normalize_and_forward_message(msg, manager)
        return True

    manager.register_channel_with_inbound(channel, forward)
    await handler.start_forwarding()
    await manager.start_dispatch()
    task = asyncio.create_task(channel._handle_raw_message(socket, json.dumps({
        'type': 'req', 'id': 'failed-stream', 'method': 'history.get',
        'params': {'session_id': 'session'},
        'trace_context': {'trace_id': '11111111-1111-1111-1111-111111111111'},
    }), {}))
    try:
        await asyncio.wait_for(agent.entered.wait(), 5)
        operation = agent.operation
        assert operation is not None and not operation.finished
        if failure == 'cancel':
            handler._stream_tasks['failed-stream'].cancel()
        elif failure == 'queue_full':
            socket.block = True
            queue = channel._send_queues[socket._jiuwen_ws_id]
            queue._maxsize = 1
            agent.release.set()
            agent.chunk({'event_type': 'chat.delta', 'content': 'first'})
            await asyncio.wait_for(socket.entered.wait(), 5)
            agent.chunk({'event_type': 'chat.delta', 'content': 'second'}, sequence=1)
            await wait_for(lambda: queue.qsize() == 1)
            agent.chunk({'event_type': 'chat.delta', 'content': 'dropped'}, sequence=2)
            await wait_for(lambda: operation.dropped_frames > 0)
            agent.chunk({'is_complete': True}, complete=True, sequence=3)
            await wait_for(lambda: 'failed-stream' not in handler._stream_tasks)
            assert not operation.finished
            socket.release.set()
        elif failure == 'writer':
            socket.fail_send = True
            agent.release.set()
            agent.chunk({'event_type': 'chat.delta', 'content': 'dropped'})
            await asyncio.wait_for(socket.entered.wait(), 5)
            await wait_for(lambda: operation.finished)
            agent.chunk({'is_complete': True}, complete=True, sequence=1)
            await wait_for(lambda: 'failed-stream' not in handler._stream_tasks)
        else:
            agent.release.set()
        await wait_for(lambda: operation.finished)
        runtime.provider.force_flush(timeout_millis=5000)
        roots = [span for span in spans(requests) if span.name == 'gw.session.message']
        assert len(roots) == 1
        attributes = {item.key: item.value.string_value for item in roots[0].attributes}
        expected = {'send': 'failed', 'cancel': 'cancelled', 'writer': 'incomplete', 'queue_full': 'incomplete'}
        assert attributes['gateway.outcome'] == expected[failure]
        if failure == 'writer':
            assert attributes['gateway.reason'] in {'send_failed', 'writer_stopped'}
        elif failure == 'queue_full':
            assert attributes['gateway.reason'] == 'queue_full'
        assert not client._message_queues
        assert telemetry.current_request_trace() is None
    finally:
        agent.release.set()
        socket.release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(manager.stop_dispatch(), 5)
        await asyncio.wait_for(handler.stop_forwarding(), 5)
        await asyncio.wait_for(channel._shutdown_all_writers(), 5)
        await asyncio.wait_for(telemetry.shutdown_gateway_telemetry(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['chat.send', 'history.get'])
async def test_stream_business_delivery_without_tracing(collector, monkeypatch, method):
    endpoint, requests = collector
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.delenv('OTEL_TRACES_SAMPLER', raising=False)
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: trace.ProxyTracerProvider())
    monkeypatch.setattr(MessageHandler, '_instance', None)

    async def no_refresh(_msg):
        pass

    monkeypatch.setattr(app_gateway, '_refresh_expired_login_credential', no_refresh)

    async def no_hook(_self, _msg):
        pass

    monkeypatch.setattr(MessageHandler, '_trigger_before_chat_request_hook', no_hook)
    runtime = telemetry.configure_gateway_telemetry(
        {'telemetry': {'enabled': False, 'exporter': 'otlp', 'protocol': 'http', 'endpoint': endpoint}},
        user_id_formatter=lambda value: mask_sensitive(f'user_id={value}').removeprefix('user_id='),
    )
    client = WebSocketAgentServerClient()
    agent = AgentSocket(client, runtime)
    client._ws = agent
    client._server_ready = True
    handler = MessageHandler(client)
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    socket = ClientSocket()
    channel._resolve_connection_user_id({'user_id': 'alice'}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id='alice', channel_id='web', app_id='default',
        agent_ref=AgentRef(mode='agent', id='default'), session_id='session',
    ))

    async def forward(msg):
        await app_gateway._normalize_and_forward_message(msg, manager)
        return True

    async def ack(ws, req_id, params, session_id, *, user_id=None):
        await channel.send_response(ws, req_id, ok=True)

    channel.register_method('chat.send', ack)
    manager.register_channel_with_inbound(channel, forward)
    await handler.start_forwarding()
    await manager.start_dispatch()
    task = asyncio.create_task(channel._handle_raw_message(socket, json.dumps({
        'type': 'req', 'id': 'stream-example', 'method': method,
        'params': {'session_id': 'session', 'content': 'hello'},
        'trace_context': {'traceparent': '00-11111111111111111111111111111111-2222222222222222-01'},
    }), {}))
    try:
        await asyncio.wait_for(agent.entered.wait(), 5)
        assert agent.operation is None
        assert telemetry.current_request_trace() is None
        assert agent.envelope.traceparent is None
        assert agent.wire['traceparent'] is None
        assert not {'traceparent', 'tracestate'} & agent.envelope.channel_context.keys()
        assert '_gateway_trace' not in json.dumps(agent.wire)
        assert agent.envelope.method == method
        assert agent.envelope.is_stream
        agent.release.set()
        agent.chunk({'event_type': 'chat.final', 'content': 'complete'})
        agent.chunk({'is_complete': True}, complete=True, sequence=1)
        await wait_for(lambda: any(frame.get('event') == 'chat.final' for frame in socket.sent))
        await wait_for(lambda: 'stream-example' not in handler._stream_tasks)
        assert not spans(requests)
        assert not channel._request_traces
        assert not client._message_queues
        if method == 'chat.send':
            assert any(frame.get('type') == 'res' and frame.get('ok') for frame in socket.sent)
    finally:
        agent.release.set()
        socket.release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(manager.stop_dispatch(), 5)
        await asyncio.wait_for(handler.stop_forwarding(), 5)
        await asyncio.wait_for(channel._shutdown_all_writers(), 5)
        await asyncio.wait_for(telemetry.shutdown_gateway_telemetry(), 5)


def provider(endpoint, name):
    result = TracerProvider(resource=Resource.create({'service.name': name}), shutdown_on_exit=False)
    result.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint + '/v1/traces')))
    return result


def exported(requests):
    return [
        (next(a.value.string_value for a in resource.resource.attributes if a.key == 'service.name'), span)
        for _, request in requests for resource in request.resource_spans
        for scope in resource.scope_spans for span in scope.spans
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sampled', 'unsampled', 'disabled'])
async def test_three_provider_wire_acceptance_and_request_isolation(collector, monkeypatch, mode):
    endpoint, requests = collector
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.setenv('OTEL_TRACES_SAMPLER', 'always_off' if mode == 'unsampled' else 'always_on')
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: trace.ProxyTracerProvider())
    browser = provider(endpoint, 'controlled-browser')
    downstream = provider(endpoint, 'controlled-downstream')
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {
        'enabled': mode != 'disabled', 'exporter': 'otlp', 'protocol': 'http', 'endpoint': endpoint,
    }})
    client = WebSocketAgentServerClient()
    captured = {}

    class AgentSocket:
        closed = False

        async def send(self, value):
            envelope = E2AEnvelope.from_dict(json.loads(value))
            captured[envelope.request_id] = envelope
            assert not {"traceparent", "tracestate"} & envelope.channel_context.keys()
            parent = TraceContextTextMapPropagator().extract({"traceparent": envelope.traceparent})
            with downstream.get_tracer('downstream').start_as_current_span('controlled.downstream', context=parent):
                response = AgentResponse(request_id=envelope.request_id, channel_id='web', ok=True,
                                         payload={'models': []})
                client._message_queues[envelope.request_id].put_nowait(
                    encode_agent_response_for_wire(response, response_id=envelope.request_id),
                )

    client._ws = AgentSocket()
    client._server_ready = True
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda message: False)
    socket = ClientSocket()
    socket.release.set()
    channel._resolve_connection_user_id({'user_id': 'alice'}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id='alice', channel_id='web', app_id='default',
        agent_ref=AgentRef(mode='agent', id='default'), session_id='session',
    ))

    async def list_models(ws, req_id, params, session_id, *, user_id=None):
        await proxy_unary_request(channel=channel, agent_client=client, ws=ws, req_id=req_id,
                                  params=params, session_id=session_id, user_id=user_id,
                                  req_method=ReqMethod.MODELS_LIST)

    channel.register_method('models.list', list_models)
    try:
        with browser.get_tracer('browser').start_as_current_span('browser.request') as span:
            browser_context = span.get_span_context()
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            async def request(rid, incoming):
                await channel._handle_raw_message(socket, json.dumps({
                    'type': 'req', 'id': rid, 'method': 'models.list', 'params': {},
                    'trace_context': incoming,
                }), {})
            await asyncio.wait_for(asyncio.gather(
                request('one', carrier), request('two', carrier), request('independent', None),
            ), 5)
        async def deliveries_done():
            while len(socket.sent) != 3 or channel._request_traces:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(deliveries_done(), 5)
        if runtime.provider:
            await asyncio.to_thread(runtime.provider.force_flush, timeout_millis=5000)
        found = exported(requests)
        assert all(frame['ok'] and frame['payload'] == {'models': []} for frame in socket.sent)
        assert not client._message_queues
        assert telemetry.current_request_trace() is None
        if mode == 'disabled':
            assert runtime.provider is None
            assert all(item.traceparent is None for item in captured.values())
            assert not any(name == 'jiuwenswarm-gateway' for name, _ in found)
        else:
            one = captured['one'].traceparent.split('-')
            two = captured['two'].traceparent.split('-')
            independent = captured['independent'].traceparent.split('-')
            assert one[1] == two[1] == format(browser_context.trace_id, '032x')
            assert one[2] != two[2]
            assert independent[1] != one[1]
            assert one[3] == two[3] == ('01' if mode == 'sampled' else '00')
            roots = [span for name, span in found if name == 'jiuwenswarm-gateway' and span.name == 'gw.session.message']
            if mode == 'unsampled':
                assert not roots
            else:
                assert len(roots) == 3, ([(name, span.name) for name, span in found], telemetry.gateway_telemetry_stats())
                browser_spans = [span for name, span in found if name == 'controlled-browser']
                children = [span for name, span in found if name == 'controlled-downstream']
                assert len(browser_spans) == 1 and len(children) == 3
                linked = [root for root in roots if root.trace_id == browser_spans[0].trace_id]
                assert len(linked) == 2
                assert all(root.parent_span_id == browser_spans[0].span_id for root in linked)
                assert all(any(child.trace_id == root.trace_id and child.parent_span_id == root.span_id
                               for child in children) for root in roots)
    finally:
        await channel._shutdown_all_writers()
        await telemetry.shutdown_gateway_telemetry()
        browser.shutdown()
        downstream.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['slow', 'reject'])
async def test_collector_failure_does_not_block_web_messages(monkeypatch, failure):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            entered.set()
            if failure == 'slow':
                release.wait(4)
            self.send_response(503 if failure == 'reject' else 200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.setenv('OTEL_TRACES_SAMPLER', 'always_on')
    monkeypatch.setenv('OTEL_BSP_SCHEDULE_DELAY', '1')
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: trace.ProxyTracerProvider())
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {
        'enabled': True, 'exporter': 'otlp', 'protocol': 'http',
        'endpoint': f'http://127.0.0.1:{server.server_port}',
    }})
    client = WebSocketAgentServerClient()

    class AgentSocket:
        closed = False

        async def send(self, value):
            envelope = E2AEnvelope.from_dict(json.loads(value))
            response = AgentResponse(request_id=envelope.request_id, channel_id='web', ok=True,
                                     payload={'models': []})
            client._message_queues[envelope.request_id].put_nowait(
                encode_agent_response_for_wire(response, response_id=envelope.request_id),
            )

    client._ws = AgentSocket()
    client._server_ready = True
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda message: False)
    socket = ClientSocket()
    socket.release.set()
    channel._resolve_connection_user_id({'user_id': 'alice'}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id='alice', channel_id='web', app_id='default',
        agent_ref=AgentRef(mode='agent', id='default'), session_id='session',
    ))

    async def list_models(ws, req_id, params, session_id, *, user_id=None):
        await proxy_unary_request(channel=channel, agent_client=client, ws=ws, req_id=req_id,
                                  params=params, session_id=session_id, user_id=user_id,
                                  req_method=ReqMethod.MODELS_LIST)

    channel.register_method('models.list', list_models)

    async def send(rid):
        await channel._handle_raw_message(socket, json.dumps({
            'type': 'req', 'id': rid, 'method': 'models.list', 'params': {},
        }), {})
        while not any(frame['id'] == rid for frame in socket.sent):
            await asyncio.sleep(0.001)

    try:
        assert runtime.provider is not None
        await asyncio.wait_for(send('first'), 3)
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.wait_for(send('during-export'), 3)
        assert all(frame['ok'] and frame['payload'] == {'models': []} for frame in socket.sent)
        assert not channel._request_traces
        assert not client._message_queues
    finally:
        release.set()
        await channel._shutdown_all_writers()
        await telemetry.shutdown_gateway_telemetry()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_real_websocket_wire_has_sdk_parent(monkeypatch, stream):
    from websockets.legacy.client import connect
    from websockets.legacy.server import serve

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    parent = telemetry.start_request_trace('parent', 'chat.send', channel='web', connection_id='c')
    captured = {}
    async def handler(ws, path):
        wire = json.loads(await ws.recv())
        captured.update(wire)
        if stream:
            response = AgentResponseChunk(request_id='child', channel_id='web', payload={'content': 'x'}, is_complete=True)
            result = encode_agent_chunk_for_wire(response, response_id='child', sequence=0)
        else:
            response = AgentResponse(request_id='child', channel_id='web', ok=True, payload={})
            result = encode_agent_response_for_wire(response, response_id='child')
        await ws.send(json.dumps(result))
        await ws.wait_closed()
    server = await serve(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    client = WebSocketAgentServerClient()
    client._ws = await connect(f'ws://127.0.0.1:{port}')
    client._server_ready = True
    client._running = True
    client._receiver_task = asyncio.create_task(client._message_receiver_loop())
    envelope = E2AEnvelope(request_id='child', method='chat.send', channel='web',
                           channel_context={'business': 'preserved'},
                           traceparent='00-11111111111111111111111111111111-2222222222222222-01')
    try:
        with telemetry.bind_request_trace(parent):
            if stream:
                assert [chunk async for chunk in client.send_request_stream(envelope)]
            else:
                assert (await client.send_request(envelope)).ok
        remote = trace.get_current_span(TraceContextTextMapPropagator().extract({'traceparent': captured['traceparent']})).get_span_context()
        assert remote.trace_id == parent.span.get_span_context().trace_id
        assert remote.span_id != parent.span.get_span_context().span_id
        assert captured['channel_context'] == {'business': 'preserved'}
        assert envelope.channel_context == {'business': 'preserved'}
        assert envelope.traceparent == '00-11111111111111111111111111111111-2222222222222222-01'
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        parent.finish('success')
        runtime.provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_invoke_real_http_wire_uses_derived_sdk_span(monkeypatch, stream):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('parent', 'chat.send', channel='web', connection_id='c')
    captured = {}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured['headers'] = {k.lower(): v for k, v in self.headers.items()}
            captured['body'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            body = b'data: {"content":"hello"}\n\n' if stream else b'{"content":"hello"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = YuanrongFrontendAgentClient(frontend_endpoint=f'http://127.0.0.1:{server.server_port}', function_version_urn='fn')
    await client.connect('')
    envelope = E2AEnvelope(request_id='child', method='chat.send', channel='web', session_id='s',
                           params={'message': 'hello'})
    try:
        with telemetry.bind_request_trace(operation):
            if stream:
                assert [chunk async for chunk in client.send_request_stream(envelope)]
            else:
                await client.send_request(envelope)
            assert telemetry.current_request_trace() is operation
        headers = captured['headers']
        remote = trace.get_current_span(TraceContextTextMapPropagator().extract(headers)).get_span_context()
        assert remote.trace_id == operation.span.get_span_context().trace_id
        assert remote.span_id != operation.span.get_span_context().span_id
        assert uuid.UUID(headers['x-trace-id']).int == remote.trace_id
        assert captured['body']['params'] == {'message': 'hello'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        operation.finish('success')
        runtime.provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 503])
async def test_unary_real_response_records_first_at_headers(monkeypatch, status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"content":"hello"}'
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = YuanrongFrontendAgentClient(
        frontend_endpoint=f'http://127.0.0.1:{server.server_port}', function_version_urn='fn',
    )
    try:
        await client.connect('')
        with telemetry.bind_request_trace(operation):
            response = await client.send_request(E2AEnvelope(
                request_id='r', method='chat.send', channel='web', session_id='s',
            ))
        assert response.ok is (status == 200)
        names = [event.name for event in operation.span.events]
        assert names.count('response.first') == 1
        assert names.index('http.response_headers') < names.index('response.first') < names.index('http.response.completed')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        operation.finish('success' if status == 200 else 'failed')
        runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_unary_actual_http_timeout_returns_expected_response(monkeypatch):
    import socket

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    release = threading.Event()
    def hold_connection():
        connection, _ = listener.accept()
        with connection:
            release.wait(5)
    thread = threading.Thread(target=hold_connection, daemon=True)
    thread.start()
    client = YuanrongFrontendAgentClient(
        frontend_endpoint=f'http://127.0.0.1:{listener.getsockname()[1]}',
        function_version_urn='fn', invoke_timeout_s=0.1,
    )
    try:
        await client.connect('')
        with telemetry.bind_request_trace(operation):
            response = await client.send_request(E2AEnvelope(
                request_id='r', channel='web', method='chat.send', session_id='s',
            ))
        assert response.request_id == 'r'
        assert response.channel_id == 'web'
        assert response.ok is False
        assert 'request timeout after 0.1s:' in response.payload['error']
        assert operation.outcome == 'timeout'
        assert not any(event.name in {'http.response_headers', 'response.first'} for event in operation.span.events)
    finally:
        release.set()
        thread.join(timeout=2)
        listener.close()
        operation.finish('timeout')
        runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_unary_connection_closed_is_not_a_response(monkeypatch):
    import socket

    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    operation = telemetry.start_request_trace('r', 'chat.send', channel='web', connection_id='c')
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(('127.0.0.1', 0))
    reserved.listen(1)
    def close_connection():
        connection, _ = reserved.accept()
        connection.close()
    thread = threading.Thread(target=close_connection, daemon=True)
    thread.start()
    client = YuanrongFrontendAgentClient(
        frontend_endpoint=f'http://127.0.0.1:{reserved.getsockname()[1]}', function_version_urn='fn',
        invoke_timeout_s=2,
    )
    try:
        await client.connect('')
        with telemetry.bind_request_trace(operation):
            response = await client.send_request(E2AEnvelope(
                request_id='r', method='chat.send', channel='web', session_id='s',
            ))
            assert telemetry.current_request_trace() is operation
        assert not response.ok
        assert operation.outcome == 'failed'
        assert not any(event.name in {'http.response_headers', 'http.response.completed', 'response.first'}
                       for event in operation.span.events)
    finally:
        reserved.close()
        thread.join(timeout=2)
        operation.finish('failed')
        runtime.provider.shutdown()
