# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preparation tracing through router ownership and transport boundaries."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from opentelemetry import context
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_gateway import telemetry
from agent_gateway.extensions.agentos.agentos_router.agent_manager import AgentManager
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse
from agent_gateway.extensions.agentos.agentos_router import router_client
from agent_gateway.extensions.agentos.agentos_router.registry_client import RegistryClient, RegistryConfig, RegistryValidationError
from agent_gateway.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from agent_gateway.extensions.yuanrong_frontend_client import SandboxInfo, YuanrongFrontendAgentClient


def _setup_trace(monkeypatch, request_id="r", method="chat.send", connection_id="c"):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_request_trace(
        request_id, method, channel="web", connection_id=connection_id,
        trace_context={"traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                       "tracestate": "vendor=value"},
    )
    return runtime, exporter, parent


def _check_trace_headers(headers, span):
    headers = {key.lower(): value for key, value in headers.items()}
    parts = headers["traceparent"].split("-")
    assert int(parts[1], 16) == span.context.trace_id
    assert int(parts[2], 16) == span.context.span_id
    assert UUID(headers["x-trace-id"]).int == span.context.trace_id
    assert headers.get("tracestate", "") == span.context.trace_state.to_header()


async def _wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 5)


async def test_disabled_gateway_tracing_preserves_external_context(monkeypatch):
    client = object.__new__(router_client.AgentOSRouterClient)
    marker = context.create_key("external-context")
    token = context.attach(context.set_value(marker, "preserved"))
    info = SimpleNamespace(agent_type="test", user_id="user", sandbox_id="sandbox")
    seen = []

    async def create_impl(self, agent_info, operation):
        seen.append(("create", context.get_value(marker), operation))
        return agent_info

    async def register(self, agent_info, operation=None):
        seen.append(("register", context.get_value(marker), operation))

    monkeypatch.setattr(router_client, "start_gateway_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_create_agent_impl", create_impl)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_register_agent", register)
    try:
        assert await client._create_agent(info) is info
        await asyncio.create_task(client._register_agent_traced(info, None))
    finally:
        context.detach(token)

    assert seen == [("create", "preserved", None), ("register", "preserved", None)]


async def test_disabled_gateway_tracing_preserves_external_context_during_ws(monkeypatch):
    client = object.__new__(router_client.AgentOSRouterClient)
    client._ws_clients = {}
    client._ws_connecting = {}
    client._ws_connect_traces = {}
    client._preparation_traces = {}
    client._ws_clients_lock = asyncio.Lock()
    marker = context.create_key("external-ws-context")
    token = context.attach(context.set_value(marker, "preserved"))
    info = SimpleNamespace(
        sandbox_id="sandbox", user_id="user", agent_type="test", metadata={"agent_port": 1234},
    )
    runtime = SimpleNamespace(info=info)
    socket = object()
    seen = []

    async def wait_running(self, *args, **kwargs):
        seen.append(context.get_value(marker))

    async def connect(self, **kwargs):
        seen.append(context.get_value(marker))
        return socket

    monkeypatch.setattr(router_client.AgentOSRouterClient, "_wait_yuanrong_running", wait_running)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_connect_ws_until_ready", connect)
    try:
        assert await client._get_ws_client(runtime) is socket
    finally:
        context.detach(token)

    assert seen == ["preserved", "preserved"]


async def test_shared_trace_non_owner_ws_links_creation(monkeypatch):

    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry_runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    telemetry_runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    first = telemetry.start_request_trace("first", "chat.send", channel="web", connection_id="c1", trace_context={"trace_id": "11111111-1111-1111-1111-111111111111"})
    second = telemetry.start_request_trace("second", "chat.send", channel="web", connection_id="c1", trace_context={"trace_id": "11111111-1111-1111-1111-111111111111"})
    with telemetry.bind_request_trace(first):
        creation = telemetry.start_gateway_trace("gw.agent.create")
    client = object.__new__(router_client.AgentOSRouterClient)
    client._ws_clients = {}
    client._ws_connecting = {}
    client._ws_connect_traces = {}
    client._ws_clients_lock = asyncio.Lock()
    owner = asyncio.create_task(asyncio.Event().wait())
    client._preparation_traces = {
        "sandbox": router_client._PreparationTrace(creation, True, owner, registered=True),
    }
    info = SimpleNamespace(sandbox_id="sandbox", user_id="user", agent_type="test", metadata={"agent_port": 1234})
    seen = []

    async def wait_running(self, *args, **kwargs):
        pass

    async def connect(self, **kwargs):
        seen.append(telemetry.current_request_trace())
        return SimpleNamespace(server_ready=True)

    monkeypatch.setattr(router_client.AgentOSRouterClient, "_wait_yuanrong_running", wait_running)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_connect_ws_until_ready", connect)
    try:
        with telemetry.bind_request_trace(second):
            await client._get_ws_client(SimpleNamespace(info=info))
        prepare = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.ws.prepare")
        assert seen[0].method == "gw.agent.ws.prepare"
        assert prepare.parent.span_id == second.span.get_span_context().span_id
        assert prepare.links[0].context.span_id == creation.span.get_span_context().span_id
        assert creation.finished and creation.outcome == "success"
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        first.finish("success")
        second.finish("success")
        await telemetry.shutdown_gateway_telemetry()


@pytest.mark.parametrize("cached", [False, True])
async def test_unconfirmed_ws_is_incomplete_after_successful_registration(monkeypatch, cached):

    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry_runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    telemetry_runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    operation = telemetry.start_gateway_trace("gw.agent.create", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    client._ws_clients = {}
    client._ws_connecting = {}
    client._ws_connect_traces = {}
    client._ws_clients_lock = asyncio.Lock()
    client._preparation_traces = {
        "sandbox": router_client._PreparationTrace(operation, True, asyncio.current_task(), registered=True),
    }
    info = SimpleNamespace(sandbox_id="sandbox", user_id="user", agent_type="test", metadata={"agent_port": 1234})
    socket = SimpleNamespace(server_ready=False)
    if cached:
        client._ws_clients["sandbox"] = socket

    async def wait_running(self, *args, **kwargs):
        pass

    async def connect(self, **kwargs):
        return socket

    monkeypatch.setattr(router_client.AgentOSRouterClient, "_wait_yuanrong_running", wait_running)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_connect_ws_until_ready", connect)
    try:
        assert await client._get_ws_client(SimpleNamespace(info=info)) is socket
        create = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.create")
        assert create.attributes["gateway.outcome"] == "incomplete"
        assert create.attributes["gateway.reason"] == "ws_readiness_unconfirmed"
        assert all(event.name != "agent.ready" for event in create.events)
    finally:
        await telemetry.shutdown_gateway_telemetry()


async def test_foreign_ws_leader_cancel_does_not_cancel_preparation(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    first = telemetry.start_request_trace("first", "chat.send", channel="web", connection_id="c1", trace_context={"trace_id": "11111111-1111-1111-1111-111111111111"})
    second = telemetry.start_request_trace("second", "chat.send", channel="web", connection_id="c1", trace_context={"trace_id": "11111111-1111-1111-1111-111111111111"})
    with telemetry.bind_request_trace(first):
        creation = telemetry.start_gateway_trace("gw.agent.create")
    client = object.__new__(router_client.AgentOSRouterClient)
    client._ws_clients = {}
    client._ws_connecting = {}
    client._ws_connect_traces = {}
    client._ws_clients_lock = asyncio.Lock()
    info = SimpleNamespace(sandbox_id="sandbox", user_id="user", agent_type="test", metadata={"agent_port": 1234})
    runtime = SimpleNamespace(info=info)
    owner_go, entered = asyncio.Event(), asyncio.Event()
    socket = SimpleNamespace(server_ready=True)
    calls = 0

    async def wait_running(self, *args, **kwargs):
        pass

    async def connect(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return socket

    async def owner_call():
        await owner_go.wait()
        with telemetry.bind_request_trace(first):
            return await client._get_ws_client(runtime)

    async def foreign_call():
        with telemetry.bind_request_trace(second):
            return await client._get_ws_client(runtime)

    monkeypatch.setattr(router_client.AgentOSRouterClient, "_wait_yuanrong_running", wait_running)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_connect_ws_until_ready", connect)
    owner = asyncio.create_task(owner_call())
    client._preparation_traces = {
        "sandbox": router_client._PreparationTrace(creation, True, owner, registered=True),
    }
    foreign = asyncio.create_task(foreign_call())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        foreign.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreign
        assert not creation.finished and creation.outcome == "success"
        owner_go.set()
        assert await asyncio.wait_for(owner, 5) is socket
        assert creation.finished and creation.outcome == "success"
    finally:
        owner.cancel()
        foreign.cancel()
        await asyncio.gather(owner, foreign, return_exceptions=True)
        first.finish("success")
        second.finish("success")
        await telemetry.shutdown_gateway_telemetry()


@pytest.mark.parametrize("skip_at", ["post", "patch"])
async def test_registry_skipped_deleted_is_incomplete(monkeypatch, skip_at):
    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_gateway_trace("gw.agent.create", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    info = SimpleNamespace(user_id="user", agent_type="test", sandbox_id="sandbox", metadata={})
    calls = []

    async def get_agent(*args, **kwargs):
        calls.append(True)
        return None if skip_at == "post" or len(calls) == 2 else object()

    async def register(*args, **kwargs):
        pass

    async def wait_running(self, *args, **kwargs):
        return {"node_ip": "127.0.0.1"}

    client._agent_manager = SimpleNamespace(key_fields=("user_id", "agent_type"), get_agent=get_agent)
    client._registry = SimpleNamespace(register_agent=register, update_instance=register)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_wait_yuanrong_running", wait_running)
    try:
        await client._register_agent(info, operation)
        assert operation.outcome == "incomplete"
        assert operation.reason == "registry_skipped_deleted"
    finally:
        operation.finish("success")
        await telemetry.shutdown_gateway_telemetry()


async def test_warmup_with_unconfirmed_ws_is_incomplete(monkeypatch):

    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry_runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    telemetry_runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = object.__new__(router_client.AgentOSRouterClient)
    client._closed = False
    runtime = SimpleNamespace(key="key", info=SimpleNamespace(sandbox_id="sandbox"))

    async def get_or_create(*args, **kwargs):
        return runtime

    async def release(*args):
        pass

    async def get_ws(self, agent_runtime):
        return SimpleNamespace(server_ready=False)

    client._agent_manager = SimpleNamespace(get_or_create_agent=get_or_create, release=release)
    monkeypatch.setattr(router_client.AgentOSRouterClient, "_get_ws_client", get_ws)
    try:
        await client._warmup_user_agent("user")
        warmup = next(span for span in exporter.get_finished_spans() if span.name == "gw.connect.warmup")
        assert warmup.attributes["gateway.outcome"] == "incomplete"
        assert warmup.attributes["gateway.reason"] == "ws_readiness_unconfirmed"
    finally:
        await telemetry.shutdown_gateway_telemetry()


class Yuanrong:
    agent_namespace = 'default'
    frontend_endpoint = 'http://127.0.0.1:8888'

    def __init__(self):
        self.creates = 0
        self.create_entered = asyncio.Event()
        self.create_release = asyncio.Event()
        self.create_release.set()
        self.waits = 0

    async def create_sandbox(self, **kwargs):
        self.creates += 1
        self.create_entered.set()
        await self.create_release.wait()
        return SandboxInfo(sandbox_id='instance-1', metadata={})

    async def disconnect(self):
        pass

    async def wait_until_running(self, instance_id, **kwargs):
        self.waits += 1
        return {'status': 'running', 'node_ip': '127.0.0.1', 'sandbox_ip': '127.0.0.2'}


class Registry:
    def __init__(self, fail=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail
        self.posts = 0
        self.patches = 0

    async def register_agent(self, info):
        self.posts += 1
        self.entered.set()
        await self.release.wait()
        if self.fail == 'post':
            raise RegistryValidationError('controlled failure', status_code=400)

    async def close(self):
        pass

    async def update_instance(self, service_id, **kwargs):
        self.patches += 1
        if self.fail == 'patch':
            raise RegistryValidationError('controlled failure', status_code=400)


class Socket:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release
        self.headers = None

    async def connect(self, uri, *, extra_headers=None):
        self.headers = extra_headers
        self.entered.set()
        await self.release.wait()

    async def send_request(self, envelope, *, timeout=None):
        return AgentResponse(request_id=envelope.request_id, channel_id=envelope.channel, ok=True)

    async def disconnect(self):
        pass


def message(request_id):
    return E2AEnvelope(request_id=request_id, method='chat.send', channel='web',
                       user_id='u1', session_id='s1', params={'query': 'hello'})


def start_message(request_id, trace_id):
    return telemetry.start_request_trace(
        request_id, 'chat.send', channel='web', connection_id='c1',
        trace_context={'trace_id': trace_id},
    )


@pytest.fixture
async def setup_router(monkeypatch, tmp_path):
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.delenv('OTEL_TRACES_SAMPLER', raising=False)
    monkeypatch.setattr(telemetry, '_runtime', None)
    runtime = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    exporter = InMemorySpanExporter()
    started = []

    class TrackingProcessor(SimpleSpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span.name)
            super().on_start(span, parent_context=parent_context)

    exporter.started = started
    runtime.provider.add_span_processor(TrackingProcessor(exporter))
    (tmp_path / 'u1').mkdir()
    yuanrong = Yuanrong()
    registry = Registry()
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()
    sockets = []

    def socket_factory():
        socket = Socket(entered, release)
        sockets.append(socket)
        return socket

    router = AgentOSRouterClient(yuanrong, registry, AgentManager(), workspace_root=str(tmp_path),
                                 ws_client_factory=socket_factory)
    yield router, yuanrong, registry, entered, release, sockets, exporter
    registry.release.set()
    yuanrong.create_release.set()
    release.set()
    await router.shutdown()
    await telemetry.shutdown_gateway_telemetry()


def named(exporter, name):
    return [span for span in exporter.get_finished_spans() if span.name == name]


@pytest.mark.parametrize('fail', [None, 'post', 'patch'])
async def test_message_finishes_before_background_registry_and_create(setup_router, fail):
    router, yuanrong, registry, entered, release, sockets, exporter = setup_router
    registry.fail = fail
    root = start_message('first', '11111111-1111-1111-1111-111111111111')
    with telemetry.bind_request_trace(root):
        response = await asyncio.wait_for(router.send_request(message('first')), 5)
    assert response.ok
    await asyncio.wait_for(registry.entered.wait(), 5)
    root.finish('success')
    assert len(named(exporter, 'gw.session.message')) == 1
    assert named(exporter, 'gw.agent.create') == []
    assert yuanrong.creates == 1 and registry.posts == 1
    second = start_message('second', '22222222-2222-2222-2222-222222222222')
    with telemetry.bind_request_trace(second):
        assert (await asyncio.wait_for(router.send_request(message('second')), 5)).ok
    second.finish('success')
    assert yuanrong.creates == 1
    registry.release.set()
    await _wait_for(lambda: bool(named(exporter, 'gw.agent.create')))
    create = named(exporter, 'gw.agent.create')[0]
    assert create.parent.span_id == root.span.get_span_context().span_id
    assert create.context.trace_id == root.span.get_span_context().trace_id
    assert create.attributes['gateway.outcome'] == ('failed' if fail else 'success')
    assert registry.patches == (0 if fail == 'post' else 1)
    assert yuanrong.waits == (1 if fail == 'post' else 2)


async def test_warmup_is_root_and_message_reuses_prepared_instance(setup_router):
    router, yuanrong, registry, entered, release, sockets, exporter = setup_router
    registry.release.set()
    caller = start_message('caller', '11111111-1111-1111-1111-111111111111')
    with telemetry.bind_request_trace(caller):
        await asyncio.wait_for(router._warmup_user_agent('u1'), 5)
    await _wait_for(lambda: bool(named(exporter, 'gw.agent.create')))
    warmup = named(exporter, 'gw.connect.warmup')[0]
    create = named(exporter, 'gw.agent.create')[0]
    assert warmup.parent is None
    assert warmup.context.trace_id != caller.span.get_span_context().trace_id
    assert create.parent.span_id == warmup.context.span_id
    assert create.context.trace_id == warmup.context.trace_id
    with telemetry.bind_request_trace(caller):
        assert (await asyncio.wait_for(router.send_request(message('caller')), 5)).ok
    assert yuanrong.creates == 1
    caller.finish('success')


async def test_shared_create_and_cancelled_ws_waiter_keep_owners(setup_router):
    router, yuanrong, registry, entered, release, sockets, exporter = setup_router
    registry.release.set()
    yuanrong.create_release.clear()
    release.clear()
    first = start_message('first', '11111111-1111-1111-1111-111111111111')
    second = start_message('second', '22222222-2222-2222-2222-222222222222')

    async def request(operation, request_id):
        with telemetry.bind_request_trace(operation):
            return await router.send_request(message(request_id))

    owner = asyncio.create_task(request(first, 'first'))
    waiter = None
    try:
        await asyncio.wait_for(yuanrong.create_entered.wait(), 5)
        waiter = asyncio.create_task(request(second, 'second'))
        await _wait_for(lambda: 'gw.agent.create.wait' in exporter.started)
        assert not waiter.done() and yuanrong.creates == 1
        yuanrong.create_release.set()
        await asyncio.wait_for(entered.wait(), 5)
        await _wait_for(lambda: bool(named(exporter, 'gw.agent.create.wait')))
        await _wait_for(lambda: 'gw.agent.ws.wait' in exporter.started)
        assert not owner.done() and not waiter.done() and len(sockets) == 1
        assert not named(exporter, 'gw.agent.create')
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert (await asyncio.wait_for(owner, 5)).ok
        create_wait = named(exporter, 'gw.agent.create.wait')[0]
        ws_wait = named(exporter, 'gw.agent.ws.wait')[0]
        assert create_wait.context.trace_id == second.span.get_span_context().trace_id
        assert create_wait.parent.span_id == second.span.get_span_context().span_id
        assert create_wait.links[0].context.span_id == first.span.get_span_context().span_id
        assert ws_wait.context.trace_id == second.span.get_span_context().trace_id
        assert ws_wait.parent.span_id == second.span.get_span_context().span_id
        assert ws_wait.links[0].context.span_id == first.span.get_span_context().span_id
        assert sockets[0].headers['X-Trace-Id'] == '11111111-1111-1111-1111-111111111111'
        assert sockets[0].headers['traceparent'].split('-')[1] == '1' * 32
        await _wait_for(lambda: bool(named(exporter, 'gw.agent.create')))
        create = named(exporter, 'gw.agent.create')
        assert len(create) == 1
        assert create[0].parent.span_id == first.span.get_span_context().span_id
        assert create[0].context.trace_id == first.span.get_span_context().trace_id
        assert yuanrong.creates == 1
    finally:
        yuanrong.create_release.set()
        release.set()
        for task in (owner, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (owner, waiter) if task is not None), return_exceptions=True)
        first.finish('success')
        second.finish('cancelled')


async def test_frontend_poll_headers_and_scope(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    observed = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({'code': 200, 'instance': {'status': 'starting' if len(observed) == 1 else 'running'}}).encode()
    def urlopen(req, timeout):
        observed.append(dict(req.header_items()))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn',
                                         wait_running_interval_s=0)
    await client.connect('')
    with telemetry.bind_request_trace(parent):
        await client.wait_until_running('i', trace_id='legacy')
        assert telemetry.current_request_trace() is parent
    spans = exporter.get_finished_spans()
    gets = [s for s in spans if s.name == 'gw.frontend.get']
    wait = next(s for s in spans if s.name == 'gw.frontend.wait_running')
    assert len(gets) == 2 and wait.attributes['poll.count'] == 2
    for headers, span in zip(observed, gets):
        _check_trace_headers(headers, span)
        assert span.parent.span_id == wait.context.span_id
    assert wait.parent.span_id == parent.span.get_span_context().span_id
    parent.finish('success')
    runtime.provider.shutdown()


async def test_registry_only_preparation_calls_have_spans(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        body = [] if request.method == 'GET' and request.url.path == '/api/instances' else {'framework': 'f', 'service_id': 's'}
        return httpx.Response(200, json=body)
    client = RegistryClient(RegistryConfig(endpoint='http://registry.test'))
    client._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handler))
    with telemetry.bind_request_trace(parent):
        await client.get_launch_spec('f', version='v')
        await client.register_instance(service_id='s', kind='k', framework='f', framework_version='v',
                                       node='n', address='a', user='u')
        await client.update_instance('s', status='running')
        await client.list_instances()
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ['registry.launch_spec', 'registry.register', 'registry.register']
    assert [s.attributes['http.request.method'] for s in spans] == ['GET', 'POST', 'PATCH']
    for request, span in zip(requests, spans):
        _check_trace_headers(request.headers, span)
        assert span.attributes['http.response.status_code'] == 200
    assert 'traceparent' not in requests[-1].headers
    await client.close()
    parent.finish('success')
    runtime.provider.shutdown()


async def test_ws_handshake_scope_is_not_inherited_by_receiver(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    captured = {}
    receiver_ran = asyncio.Event()
    class WS:
        async def recv(self):
            return json.dumps({'type': 'event', 'event': 'connection.ack', 'payload': {'readiness': 'AGENT_READY'}})
        async def close(self): pass
    async def connect(uri, **kwargs):
        captured['headers'] = dict(kwargs['extra_headers'])
        return WS()
    async def receiver(self):
        captured['receiver'] = telemetry.current_request_trace()
        receiver_ran.set()
    monkeypatch.setattr('websockets.legacy.client.connect', connect)
    monkeypatch.setattr(WebSocketAgentServerClient, '_message_receiver_loop', receiver)
    client = WebSocketAgentServerClient()
    with telemetry.bind_request_trace(parent):
        await client.connect('ws://localhost', extra_headers={'Authorization': 'secret', 'X-Trace-Id': 'legacy'})
        await receiver_ran.wait()
    span = next(s for s in exporter.get_finished_spans() if s.name == 'gw.agent.ws.connect')
    _check_trace_headers(captured['headers'], span)
    assert captured['headers']['Authorization'] == 'secret'
    assert captured['receiver'] is None and client.server_ready
    assert client._extra_headers['X-Trace-Id'] == 'legacy'
    await client.disconnect()
    parent.finish('success')
    runtime.provider.shutdown()



async def test_create_retry_and_failure_preserve_trace(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    headers = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"code": 200, "instance_id": "i"}'
    def urlopen(req, timeout):
        headers.append(dict(req.header_items()))
        if len(headers) == 1:
            raise TimeoutError('timeout')
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    options = dict(namespace='n', name='name', workspace='/tmp',
                   runtime_spec={'runtime': 'docker', 'rootfs': {'imageurl': 'image'}})
    with telemetry.bind_request_trace(parent):
        with pytest.raises(Exception):
            await client.create_sandbox(**options)
        info = await client.create_sandbox(**options)
    assert UUID(info.metadata["trace_id"]).int == parent.span.get_span_context().trace_id
    assert info.metadata["trace_id"] == {k.lower(): v for k, v in headers[1].items()}["x-trace-id"]
    spans = [s for s in exporter.get_finished_spans() if s.name == 'gw.frontend.create']
    assert len(spans) == 2
    assert spans[0].attributes['gateway.outcome'] == 'timeout'
    for header, span in zip(headers, spans):
        _check_trace_headers(header, span)
        assert span.parent.span_id == parent.span.get_span_context().span_id
    parent.finish('success')
    runtime.provider.shutdown()

async def test_registry_timeout_and_untraced_calls(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(200, json={'framework': 'f'})
    client = RegistryClient(RegistryConfig(endpoint='http://registry.test'))
    client._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handler))
    with telemetry.bind_request_trace(parent):
        with pytest.raises(Exception):
            await client.get_launch_spec('f')
    await client.get_launch_spec('f')
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].attributes['gateway.outcome'] == 'timeout'
    _check_trace_headers(requests[0].headers, spans[0])
    assert 'traceparent' not in requests[1].headers
    await client.close()
    parent.finish('success')
    runtime.provider.shutdown()

async def test_ws_cancel_records_cancelled_without_changing_exception(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    entered = asyncio.Event()
    async def connect(uri, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr('websockets.legacy.client.connect', connect)
    client = WebSocketAgentServerClient()
    with telemetry.bind_request_trace(parent):
        task = asyncio.create_task(client.connect('ws://localhost'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    span = next(s for s in exporter.get_finished_spans() if s.name == 'gw.agent.ws.connect')
    assert span.attributes['gateway.outcome'] == 'cancelled'
    parent.finish('success')
    runtime.provider.shutdown()


async def test_ws_reconnect_does_not_reuse_router_sdk_headers(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    calls = []
    class WS:
        async def recv(self):
            return json.dumps({'type': 'event', 'event': 'connection.ack', 'payload': {'status': 'ready'}})
        async def close(self): pass
    async def connect(uri, **kwargs):
        calls.append(dict(kwargs.get('extra_headers', [])))
        return WS()
    async def receiver(self):
        return None
    monkeypatch.setattr('websockets.legacy.client.connect', connect)
    monkeypatch.setattr(WebSocketAgentServerClient, '_message_receiver_loop', receiver)
    client = WebSocketAgentServerClient()
    with telemetry.bind_request_trace(parent):
        router_headers = telemetry.inject_request_headers({'Authorization': 'Bearer token'})
        await client.connect('ws://localhost', extra_headers=router_headers)
    first_span = next(s for s in exporter.get_finished_spans() if s.name == 'gw.agent.ws.connect')
    _check_trace_headers(calls[0], first_span)
    client._ws = None
    await client._ensure_connected_for_request()
    assert calls[1] == {'Authorization': 'Bearer token'}
    await client.disconnect()
    parent.finish('success')
    runtime.provider.shutdown()
