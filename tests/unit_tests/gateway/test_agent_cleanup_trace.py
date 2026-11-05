# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cleanup trace behavior across the router, sweep and transport layers."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_gateway import telemetry
from agent_gateway.extensions.agentos.agentos_router import agent_manager
from agent_gateway.extensions.agentos.agentos_router.agent_manager import AgentManager
from agent_gateway.extensions.agentos.agentos_router import router_client
from agent_gateway.extensions.agentos.agentos_router.registry_client import (
    InstanceRecord, RegistryClient, RegistryConfig, instance_service_id,
)
from agent_gateway.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from agent_gateway.extensions.agentos.agentos_router.stale_cleanup import cleanup_stale_sandboxes
from agent_gateway.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient


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


async def test_delete_records_swallowed_failure_and_preserves_parent(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_gateway_trace("request", root=True)
    info = SimpleNamespace(user_id="user", agent_type="test", sandbox_id="sandbox", agent_id="agent", metadata={})
    client = object.__new__(router_client.AgentOSRouterClient)
    client._agent_manager = SimpleNamespace(get_agent=AsyncMock(return_value=SimpleNamespace(info=info)), delete_agent=AsyncMock())
    client._yuanrong = SimpleNamespace(delete_sandbox=AsyncMock(side_effect=TimeoutError()))
    client._registry = SimpleNamespace(unregister_agent=AsyncMock())
    client._web_ready_sandboxes = set()
    socket = SimpleNamespace(disconnect=AsyncMock())
    client._ws_clients = {"sandbox": socket}
    client._ws_connecting = {}
    client._ws_connect_traces = {}
    client._ws_clients_lock = asyncio.Lock()
    try:
        with telemetry.bind_request_trace(parent):
            assert await client.delete_agent("user", "test") is True
        spans = exporter.get_finished_spans()
        deletion = next(span for span in spans if span.name == "gw.agent.delete")
        close = next(span for span in spans if span.name == "gw.agent.ws.close")
        assert deletion.parent.span_id == parent.span.get_span_context().span_id
        assert close.parent.span_id == deletion.context.span_id
        assert deletion.attributes["gateway.outcome"] == "timeout"
        assert deletion.attributes["cleanup.ws.close.outcome"] == "success"
        assert deletion.attributes["cleanup.frontend.delete.outcome"] == "timeout"
        assert deletion.attributes["cleanup.runtime.remove.outcome"] == "success"
        assert deletion.attributes["cleanup.registry.unregister.outcome"] == "incomplete"
        assert deletion.attributes["cleanup.result"] == "partial"
        assert parent.outcome == "success"
        client._agent_manager.delete_agent.assert_awaited_once()
        client._registry.unregister_agent.assert_awaited_once()
    finally:
        parent.finish("success")
        await telemetry.shutdown_gateway_telemetry()


async def test_idle_guard_rejections_create_no_trace_and_preserve_parent(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_gateway_trace("request", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    client._sandbox_idle_timeout_seconds = 30
    client._agent_manager = SimpleNamespace(
        key_fields=["user_id", "agent_type"],
        list_keys=AsyncMock(return_value=[("user", "test")]),
        pop_if_idle=AsyncMock(return_value=None),
    )
    try:
        with telemetry.bind_request_trace(parent):
            for _ in range(30):
                assert await client._reap_idle_once() == 0
                assert telemetry.current_request_trace() is parent
            assert exporter.get_finished_spans() == ()
        assert client._agent_manager.pop_if_idle.await_count == 30
    finally:
        parent.finish("success")
        await telemetry.shutdown_gateway_telemetry()

async def test_not_running_fallback_unregister_has_one_delete_span(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    info = SimpleNamespace(user_id="user", agent_type="test", sandbox_id="sandbox", agent_id="agent", metadata={})
    client = object.__new__(router_client.AgentOSRouterClient)
    client._agent_manager = SimpleNamespace(key_fields=["user_id", "agent_type"], get_agent=AsyncMock(return_value=None))
    client._registry = SimpleNamespace(unregister_agent=AsyncMock())
    try:
        await client._cleanup_agent_after_create_not_running(info, exc=RuntimeError())
        deletions = [span for span in exporter.get_finished_spans() if span.name == "gw.agent.delete"]
        assert len(deletions) == 1
        assert deletions[0].attributes["source"] == "create_not_running"
        assert deletions[0].attributes["cleanup.guard.reason"] == "no_runtime"
        assert deletions[0].attributes["cleanup.registry.unregister.reason"] == "result_unconfirmed"
        client._registry.unregister_agent.assert_awaited_once()
    finally:
        await telemetry.shutdown_gateway_telemetry()


async def test_delayed_cleanup_online_is_a_new_skipped_trace(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_gateway_trace("request", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    client._disconnect_cleanup_timeout_seconds = 0
    client._agent_manager = SimpleNamespace(get_user_connection_count=lambda _: 1)
    try:
        with telemetry.bind_request_trace(parent):
            await client._delayed_cleanup("user")
        deletion = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.delete")
        assert deletion.parent is None
        assert deletion.attributes["cleanup.result"] == "skipped"
        assert deletion.attributes["cleanup.guard.reason"] == "still_online"
    finally:
        parent.finish("success")
        await telemetry.shutdown_gateway_telemetry()

async def test_independent_cleanup_jobs_do_not_inherit_request_or_cleanup_context(monkeypatch):
    from opentelemetry import trace

    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    parent = telemetry.start_gateway_trace("request", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    client._closed = False
    client._disconnect_cleanup_timeout_seconds = 30
    client._sandbox_idle_timeout_seconds = 30
    client._pending_cleanups = {}
    client._background_tasks = set()
    client._idle_reaper_task = None
    client._stale_cleanup_task = None
    client._registry = SimpleNamespace(enabled=True)
    client._agent_manager = SimpleNamespace(
        decrement_user_connections=lambda _: 0,
        get_user_connection_count=lambda _: 0,
    )
    client._ws_clients = {}
    client._ws_clients_lock = asyncio.Lock()
    seen = []

    async def observe(*args):
        seen.append((telemetry.current_request_trace(), telemetry.current_cleanup_trace(),
                     trace.get_current_span().get_span_context().is_valid))

    client._delayed_cleanup = observe
    client._idle_reaper_loop = observe
    client._startup_cleanup_sandboxes = observe
    try:
        with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
            await client._on_channel_event(SimpleNamespace(user_id="one", event_type="disconnected"))
            await client._handle_agent_ws_closed(
                object(), user_id="two", session_id="session", agent_type="test", instance_id="sandbox", reason="closed",
            )
            client._ensure_idle_reaper_task()
            client._ensure_stale_cleanup_task()
            tasks = [*client._pending_cleanups.values(), client._idle_reaper_task, client._stale_cleanup_task]
            await asyncio.gather(*tasks)
            assert telemetry.current_request_trace() is parent
            assert telemetry.current_cleanup_trace() is parent
        assert seen == [(None, None, False)] * 4
    finally:
        parent.finish("success")
        await telemetry.shutdown_gateway_telemetry()


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield runtime, exporter
    runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_sweep_each_instance_has_independent_result_and_preserves_processed_count(tracing):
    _, exporter = tracing
    calls = []
    records = [InstanceRecord(service_id=instance_service_id(user, "jiuwenswarm"), framework="jiuwenswarm",
                              user=user, instance_id=instance)
               for user, instance in [("u1", "removed"), ("u2", "delete-failed"),
                                      ("u3", "cas-skipped"), ("u4", "live")]]

    class Frontend:
        async def delete_sandbox(self, instance):
            calls.append(("delete", instance))
            if instance == "delete-failed":
                raise RuntimeError("controlled failure")
            telemetry.record_cleanup_result("frontend.delete", "success")

    class Registry:
        enabled = True
        async def list_instances(self, *, include_unhealthy):
            assert include_unhealthy
            return records
        async def unregister_instance(self, service, *, expected_instance_id):
            calls.append(("unregister", expected_instance_id))
            if expected_instance_id == "cas-skipped":
                return {"deleted": False, "reason": "instance_id_mismatch"}
            return {"deleted": True}

    manager = SimpleNamespace(list_all_agents=AsyncMock(return_value=[SimpleNamespace(info=SimpleNamespace(
        user_id="u4", agent_type="jiuwenswarm", sandbox_id="live"))]))
    parent = telemetry.start_request_trace("message", "chat.send", channel="web", connection_id="c")
    with telemetry.bind_request_trace(parent):
        assert await cleanup_stale_sandboxes(yuanrong=Frontend(), registry=Registry(), agent_manager=manager) == 4
        assert telemetry.current_request_trace() is parent
        assert telemetry.current_cleanup_trace() is None
    spans = [span for span in exporter.get_finished_spans() if span.name == "gw.agent.sweep"]
    assert len(spans) == len({span.context.trace_id for span in spans}) == 4
    assert all(span.parent is None and span.attributes["source"] == "startup.sweep" for span in spans)
    results = {span.attributes["instance_id"]: span for span in spans}
    assert results["removed"].attributes["cleanup.result"] == "success"
    assert results["delete-failed"].attributes["gateway.outcome"] == "failed"
    assert results["delete-failed"].attributes["cleanup.result"] == "partial"
    assert results["cas-skipped"].attributes["cleanup.result"] == "partial"
    assert results["cas-skipped"].attributes["cleanup.registry.unregister.reason"] == "instance_id_mismatch"
    assert results["live"].attributes["cleanup.result"] == "skipped"
    assert ("delete", "live") not in calls and ("unregister", "live") not in calls
    assert ("unregister", "delete-failed") in calls
    parent.complete()


@pytest.mark.asyncio
async def test_sweep_unconfirmed_delete_is_not_success(tracing):
    _, exporter = tracing
    record = InstanceRecord(service_id="service", framework="jiuwenswarm", user="user", instance_id="instance")
    registry = SimpleNamespace(enabled=True, list_instances=AsyncMock(return_value=[record]),
                               unregister_instance=AsyncMock(return_value={"deleted": True}))
    manager = SimpleNamespace(list_all_agents=AsyncMock(return_value=[]))
    frontend = SimpleNamespace(delete_sandbox=AsyncMock(return_value=None))
    assert await cleanup_stale_sandboxes(yuanrong=frontend, registry=registry, agent_manager=manager) == 1
    span = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.sweep")
    assert span.attributes["gateway.outcome"] == "incomplete"
    assert span.attributes["cleanup.frontend.delete.reason"] == "delete_result_unconfirmed"
    assert span.attributes["cleanup.result"] == "partial"


def test_cleanup_scope_does_not_contaminate_message_and_clears_background_context(tracing):
    parent = telemetry.start_request_trace("message", "chat.send", channel="web", connection_id="c")
    with telemetry.bind_request_trace(parent):
        with telemetry.trace_operation("gw.agent.delete") as cleanup, telemetry.bind_cleanup_trace(cleanup):
            telemetry.record_cleanup_result("frontend.delete", "failed", reason="network")
            telemetry.record_cleanup_result("frontend.delete", "success")
            assert telemetry.get_cleanup_result("frontend.delete") == "failed"
            with telemetry.bind_request_trace(None):
                assert telemetry.current_cleanup_trace() is None
                telemetry.record_cleanup_result("registry.unregister", "failed")
            assert telemetry.current_cleanup_trace() is cleanup
            telemetry.record_cleanup_result("registry.unregister", "success")
        assert cleanup.outcome == "failed" and cleanup.span.attributes["cleanup.result"] == "partial"
        assert parent.outcome == "success" and not parent.finished
        assert telemetry.current_cleanup_trace() is None
    parent.complete()


def test_cleanup_result_is_available_with_sampling_disabled(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    try:
        with telemetry.trace_operation("gw.agent.delete") as operation, telemetry.bind_cleanup_trace(operation):
            assert not operation.span.is_recording()
            telemetry.record_cleanup_result("registry.unregister", "skipped", reason="instance_id_mismatch")
            assert telemetry.get_cleanup_result("registry.unregister") == "skipped"
        assert operation.outcome == "incomplete"
    finally:
        runtime.provider.shutdown()


def test_cleanup_observation_failure_does_not_change_work_or_leak_span(tracing, monkeypatch):
    _, exporter = tracing

    def fail_attribute(*args):
        raise RuntimeError("controlled SDK failure")

    with telemetry.trace_operation("gw.agent.delete") as operation, telemetry.bind_cleanup_trace(operation):
        monkeypatch.setattr(operation.span, "set_attribute", fail_attribute)
        telemetry.record_cleanup_result("frontend.delete", "failed", reason="network")
        telemetry.record_cleanup_result("registry.unregister", "success")
        assert operation.outcome == "failed"
        assert telemetry.get_cleanup_result("registry.unregister") == "success"
    assert operation.finished
    assert any(span.name == "gw.agent.delete" for span in exporter.get_finished_spans())


@pytest.fixture
async def cleanup_setup(monkeypatch):
    monkeypatch.setattr(telemetry, '_runtime', None)
    monkeypatch.delenv('OTEL_ENABLED', raising=False)
    monkeypatch.delenv('OTEL_TRACES_SAMPLER', raising=False)
    tracing = telemetry.configure_gateway_telemetry({'telemetry': {'enabled': True, 'exporter': 'none'}})
    exporter = InMemorySpanExporter()
    tracing.provider.add_span_processor(SimpleSpanProcessor(exporter))
    events = []
    registry_response = {'status': 200, 'body': {'deleted': True}}
    frontend = YuanrongFrontendAgentClient(frontend_endpoint='http://frontend.test', function_version_urn='fn')
    await frontend.connect('')
    response = {'status': 200, 'body': {'code': 200}}
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()

    def delete(instance_id, trace_id):
        events.append(('frontend', instance_id))
        return response['status'], json.dumps(response['body'])

    monkeypatch.setattr(frontend, '_do_agent_delete', delete)
    registry = RegistryClient(RegistryConfig(endpoint='http://registry.test'))

    async def handle(request):
        events.append(('registry', request.method))
        assert await manager.get_agent('u1', 'jiuwenswarm') is None
        return httpx.Response(registry_response['status'], json=registry_response['body'])

    registry._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handle))
    manager = AgentManager()
    router = AgentOSRouterClient(frontend, registry, manager, disconnect_cleanup_timeout_seconds=0.01,
                                 sandbox_idle_timeout_seconds=0.01)

    async def creator(info):
        info.sandbox_id = 'instance-1'
        return info

    async def seed(acquire=False):
        return await manager.get_or_create_agent('u1', 'jiuwenswarm', creator=creator, acquire=acquire)

    class Socket:
        async def disconnect(self):
            events.append(('ws', 'close'))
            entered.set()
            await release.wait()

    router._ws_clients['instance-1'] = Socket()
    yield SimpleNamespace(router=router, manager=manager, frontend=frontend, registry=registry,
                          events=events, exporter=exporter, response=response, seed=seed,
                          entered=entered, release=release, registry_response=registry_response)
    release.set()
    await router.shutdown()
    await telemetry.shutdown_gateway_telemetry()


def spans(setup, name):
    return [span for span in setup.exporter.get_finished_spans() if span.name == name]


def message_trace():
    return telemetry.start_request_trace('r1', 'chat.send', channel='web', connection_id='c1',
                                         trace_context={'trace_id': '11111111-1111-1111-1111-111111111111'})


@pytest.mark.parametrize('fail', [False, True])
async def test_explicit_delete_observes_real_leaf_results_and_preserves_order(cleanup_setup, fail):
    s = cleanup_setup
    await s.seed()
    if fail:
        s.response.update(status=500, body={'code': 500, 'message': 'failure'})
    parent = message_trace()
    with telemetry.bind_request_trace(parent):
        assert await s.router.delete_agent('u1', 'jiuwenswarm') is True
    parent.finish('success')
    assert s.events == [('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.parent.span_id == parent.span.get_span_context().span_id
    assert delete.attributes['cleanup.frontend.delete.outcome'] == ('failed' if fail else 'success')
    assert delete.attributes['cleanup.registry.unregister.outcome'] == 'success'
    assert delete.attributes['gateway.outcome'] == ('failed' if fail else 'success')
    assert len(spans(s, 'gw.frontend.delete')) == len(spans(s, 'registry.unregister')) == 1


@pytest.mark.parametrize('fail', [False, True])
async def test_idle_checks_only_trace_actual_reclaim(cleanup_setup, monkeypatch, fail):
    s = cleanup_setup
    held = await s.seed(acquire=True)
    clock = [held.last_active_at + 3601]
    monkeypatch.setattr(agent_manager, 'time', SimpleNamespace(time=lambda: clock[0]))
    parent = message_trace()
    original_pop = s.manager.pop_if_idle

    async def pop(*args):
        runtime = await original_pop(*args)
        if runtime is not None:
            s.events.append(('manager', 'pop'))
        return runtime

    s.manager.pop_if_idle = pop
    if fail:
        s.response.update(status=500, body={'code': 500, 'message': 'failure'})
    with telemetry.bind_request_trace(parent):
        for _ in range(30):
            assert await s.router._reap_idle_once() == 0
        assert s.events == []
        assert s.exporter.get_finished_spans() == ()
        assert (await s.manager.get_agent('u1', 'jiuwenswarm')).task_count == 1
        await s.manager.release(held.key)
        s.router._sandbox_idle_timeout_seconds = 3600
        for _ in range(30):
            assert await s.router._reap_idle_once() == 0
        assert s.events == []
        assert s.exporter.get_finished_spans() == ()
        clock[0] += 3601
        assert await s.router._reap_idle_once() == 1
        assert telemetry.current_request_trace() is parent
    assert s.events == [('manager', 'pop'), ('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None
    finished = s.exporter.get_finished_spans()
    assert len(finished) == 4
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.parent is None
    assert delete.context.trace_id != parent.span.get_span_context().trace_id
    assert delete.attributes['source'] == 'idle_reaper'
    assert delete.attributes['gateway.outcome'] == ('failed' if fail else 'success')
    assert delete.attributes['cleanup.result'] == ('partial' if fail else 'success')
    for name in ('gw.agent.ws.close', 'gw.frontend.delete', 'registry.unregister'):
        leaf = spans(s, name)
        assert len(leaf) == 1
        assert leaf[0].parent.span_id == delete.context.span_id
        assert leaf[0].context.trace_id == delete.context.trace_id
    parent.finish('success')


async def test_delayed_cleanup_is_independent_of_finished_message(cleanup_setup):
    s = cleanup_setup
    await s.seed()
    await asyncio.sleep(0.03)
    parent = message_trace()
    with telemetry.bind_request_trace(parent):
        await s.router._on_channel_event(SimpleNamespace(user_id='u1', event_type='disconnected'))
        task = s.router._pending_cleanups['u1']
        parent.finish('success')
        await asyncio.wait_for(task, 5)
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.parent is None
    assert delete.context.trace_id != parent.span.get_span_context().trace_id
    assert delete.attributes['source'] == 'disconnect_cleanup'
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None


async def test_network_failure_cleanup_is_shielded_and_stays_in_trace(cleanup_setup):
    s = cleanup_setup
    runtime = await s.seed()
    s.release.clear()
    parent = message_trace()
    with telemetry.bind_request_trace(parent):
        task = asyncio.create_task(s.router._cleanup_agent_on_network_failure(runtime, reason='network_error'))
        await asyncio.wait_for(s.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.events == [('ws', 'close')]
        s.release.set()
        await _wait_for(lambda: spans(s, 'gw.agent.delete'))
    parent.finish('failed')
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.parent.span_id == parent.span.get_span_context().span_id
    assert delete.context.trace_id == parent.span.get_span_context().trace_id
    assert delete.attributes['source'] == 'network_failure'
    assert s.events == [('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None




async def test_registry_cas_skip_survives_none_return(cleanup_setup, monkeypatch):
    s = cleanup_setup
    await s.seed()
    s.registry_response.update(status=409, body={'detail': 'mismatch'})

    async def unregister_agent(agent_id, *, user_id, agent_type):
        result = await s.registry.unregister_instance('service-1', expected_instance_id='instance-1')
        assert result['deleted'] is False
        return None

    monkeypatch.setattr(s.registry, 'unregister_agent', unregister_agent)
    parent = message_trace()
    with telemetry.bind_request_trace(parent):
        assert await s.router.delete_agent('u1', 'jiuwenswarm') is True
    parent.finish('success')
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.attributes['cleanup.registry.unregister.outcome'] == 'skipped'
    assert delete.attributes['cleanup.registry.unregister.reason'] == 'instance_id_mismatch'
    assert delete.attributes['gateway.outcome'] != 'success'
    assert s.events == [('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None


async def test_idle_success_pops_before_releasing_resources(cleanup_setup):
    s = cleanup_setup
    await s.seed()
    await asyncio.sleep(0.03)
    original = s.frontend.delete_sandbox

    async def delete(instance_id):
        assert await s.manager.get_agent('u1', 'jiuwenswarm') is None
        await original(instance_id)

    s.frontend.delete_sandbox = delete
    assert await s.router._reap_idle_once() == 1
    assert s.events == [('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    delete_span = spans(s, 'gw.agent.delete')[0]
    assert delete_span.parent is None
    assert delete_span.attributes['source'] == 'idle_reaper'
    assert delete_span.attributes['cleanup.frontend.delete.outcome'] == 'success'
    assert delete_span.attributes['cleanup.registry.unregister.outcome'] == 'success'



@pytest.mark.parametrize('unavailable', [False, True])
async def test_inline_failure_cleanup_stays_in_message_trace(cleanup_setup, unavailable):
    s = cleanup_setup
    runtime = await s.seed()
    parent = message_trace()
    with telemetry.bind_request_trace(parent):
        if unavailable:
            await s.router._cleanup_agent_on_instance_unavailable(runtime, exc=RuntimeError('unavailable'))
        else:
            await s.router._cleanup_agent_on_network_failure(runtime, reason='network_error')
    parent.finish('failed')
    delete = spans(s, 'gw.agent.delete')[0]
    assert delete.context.trace_id == parent.span.get_span_context().trace_id
    assert delete.parent.span_id == parent.span.get_span_context().span_id
    assert delete.attributes['source'] == ('instance_unavailable' if unavailable else 'network_failure')
    assert s.events == [('ws', 'close'), ('frontend', 'instance-1'), ('registry', 'DELETE')]
    assert await s.manager.get_agent('u1', 'jiuwenswarm') is None


@pytest.mark.parametrize('status,outcome,reason', [(200, 'success', 'removed'), (404, 'skipped', 'not_found')])
async def test_frontend_delete_wire_and_cleanup(monkeypatch, status, outcome, reason):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"code": 200}'
    def urlopen(req, timeout):
        seen.append(dict(req.header_items()))
        response = Response()
        response.status = status
        return response
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
        await client.delete_sandbox('i', trace_id='legacy')
    span = next(s for s in exporter.get_finished_spans() if s.name == 'gw.frontend.delete')
    _check_trace_headers(seen[0], span)
    assert span.attributes['http.request.method'] == 'DELETE'
    assert span.attributes['http.response.status_code'] == status
    parent.finish('success')
    root = next(s for s in exporter.get_finished_spans() if s.context.span_id == parent.span.get_span_context().span_id)
    assert root.attributes['cleanup.frontend.delete.outcome'] == outcome
    assert root.attributes['cleanup.frontend.delete.reason'] == reason
    runtime.provider.shutdown()


@pytest.mark.parametrize('status,body,outcome,reason', [
    (200, {'deleted': True}, 'success', 'removed'),
    (200, {}, 'skipped', 'unconfirmed'),
    (200, {'deleted': False}, 'skipped', 'instance_id_mismatch'),
    (404, {}, 'skipped', 'not_found'),
    (409, {}, 'skipped', 'instance_id_mismatch'),
])
async def test_registry_delete_cas_and_cleanup(monkeypatch, status, body, outcome, reason):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body)
    client = RegistryClient(RegistryConfig(endpoint='http://registry.test'))
    client._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handler))
    with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
        result = await client.unregister_instance('service', expected_instance_id='instance')
    assert requests[0].url.params['instance_id'] == 'instance'
    if 'deleted' in result:
        assert result['deleted'] is (status == 200 and body.get('deleted') is True)
    else:
        assert result == {}
    span = next(s for s in exporter.get_finished_spans() if s.name == 'registry.unregister')
    _check_trace_headers(requests[0].headers, span)
    assert span.attributes['http.response.status_code'] == status
    parent.finish('success')
    root = next(s for s in exporter.get_finished_spans() if s.context.span_id == parent.span.get_span_context().span_id)
    assert root.attributes['cleanup.registry.unregister.outcome'] == outcome
    assert root.attributes['cleanup.registry.unregister.reason'] == reason
    await client.close()
    runtime.provider.shutdown()


async def test_untraced_delete_preserves_legacy_headers(monkeypatch):
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"code": 200}'
    def urlopen(req, timeout):
        seen.append({k.lower(): v for k, v in req.header_items()})
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    await client.delete_sandbox('i', trace_id='legacy')
    assert seen[0]['x-trace-id'] == 'legacy'
    assert 'traceparent' not in seen[0]


@pytest.mark.parametrize('kind', ['timeout', 'failed', 'cancelled'])
async def test_frontend_delete_errors_are_reported_without_changing_exception(monkeypatch, kind):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    async def run_thread(*args):
        if kind == 'cancelled':
            raise asyncio.CancelledError()
        if kind == 'timeout':
            raise TimeoutError('timeout')
        raise ValueError('failed')
    monkeypatch.setattr(asyncio, 'to_thread', run_thread)
    client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await client.connect('')
    exception = {'cancelled': asyncio.CancelledError, 'timeout': TimeoutError, 'failed': ValueError}[kind]
    with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
        with pytest.raises(exception):
            await client.delete_sandbox('i')
        assert telemetry.get_cleanup_result('frontend.delete') == kind
    span = next(s for s in exporter.get_finished_spans() if s.name == 'gw.frontend.delete')
    assert span.attributes['gateway.outcome'] == kind
    parent.finish('success')
    runtime.provider.shutdown()


@pytest.mark.parametrize('kind', ['timeout', 'failed', 'cancelled'])
async def test_registry_delete_errors_are_reported_without_changing_exception(monkeypatch, kind):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    def handler(request):
        if kind == 'cancelled':
            raise asyncio.CancelledError()
        if kind == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(500, json={})
    client = RegistryClient(RegistryConfig(endpoint='http://registry.test'))
    client._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handler))
    with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
        with pytest.raises(asyncio.CancelledError if kind == 'cancelled' else Exception):
            await client.unregister_instance('service', expected_instance_id='instance')
        assert telemetry.get_cleanup_result('registry.unregister') == kind
    span = next(s for s in exporter.get_finished_spans() if s.name == 'registry.unregister')
    assert span.attributes['gateway.outcome'] == kind
    await client.close()
    parent.finish('success')
    runtime.provider.shutdown()


@pytest.mark.parametrize('transport', ['frontend', 'registry'])
async def test_http_success_survives_sdk_attribute_failure(monkeypatch, transport):
    runtime, exporter, parent = _setup_trace(monkeypatch)
    failed_spans = []

    def fail_leaf_attribute():
        leaf = telemetry.current_request_trace()
        assert leaf is not None and leaf is not parent
        failed_spans.append(leaf.span)
        def fail(*args, **kwargs):
            raise RuntimeError('SDK setter failed')
        monkeypatch.setattr(leaf.span, 'set_attribute', fail)

    if transport == 'frontend':
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"code": 200}'
        def urlopen(req, timeout):
            fail_leaf_attribute()
            return Response()
        monkeypatch.setattr('urllib.request.urlopen', urlopen)
        client = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
        await client.connect('')
        with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
            assert await client.delete_sandbox('instance') is None
            assert telemetry.get_cleanup_result('frontend.delete') == 'success'
    else:
        def handler(request):
            fail_leaf_attribute()
            assert request.url.params['instance_id'] == 'instance'
            return httpx.Response(200, json={'service_id': 'service', 'deleted': True})
        client = RegistryClient(RegistryConfig(endpoint='http://registry.test'))
        client._http = httpx.AsyncClient(base_url='http://registry.test', transport=httpx.MockTransport(handler))
        with telemetry.bind_request_trace(parent), telemetry.bind_cleanup_trace(parent):
            result = await client.unregister_instance('service', expected_instance_id='instance')
            assert result == {'service_id': 'service', 'deleted': True}
            assert telemetry.get_cleanup_result('registry.unregister') == 'success'
        await client.close()
    assert len(failed_spans) == 1
    assert any(span.context.span_id == failed_spans[0].get_span_context().span_id
               for span in exporter.get_finished_spans())
    parent.finish('success')
    runtime.provider.shutdown()


async def test_disabled_registry_unregister_agent_is_reported_by_router(monkeypatch):
    from agent_gateway.extensions.agentos.agentos_router.agent_manager import AgentManager
    from agent_gateway.extensions.agentos.agentos_router.router_client import AgentOSRouterClient

    runtime, exporter, parent = _setup_trace(monkeypatch)
    frontend = YuanrongFrontendAgentClient(frontend_endpoint='http://localhost', function_version_urn='fn')
    await frontend.connect('')
    monkeypatch.setattr(frontend, '_do_agent_delete', lambda instance_id, trace_id: (200, '{"code": 200}'))
    registry = RegistryClient(RegistryConfig())
    manager = AgentManager()
    router = AgentOSRouterClient(frontend, registry, manager)
    async def creator(info):
        info.sandbox_id = 'instance'
        return info
    await manager.get_or_create_agent('user', 'jiuwenswarm', creator=creator)
    with telemetry.bind_request_trace(parent):
        assert await router.delete_agent('user', 'jiuwenswarm') is True
    leaf = next(s for s in exporter.get_finished_spans() if s.name == 'gw.agent.delete')
    assert leaf.attributes['cleanup.registry.unregister.outcome'] == 'skipped'
    assert leaf.attributes['cleanup.registry.unregister.reason'] == 'disabled'
    assert not any(s.name == 'registry.unregister' for s in exporter.get_finished_spans())
    await router.shutdown()
    parent.finish('success')
    runtime.provider.shutdown()
