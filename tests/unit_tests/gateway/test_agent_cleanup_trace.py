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

from jiuwenswarm.gateway import telemetry
from jiuwenswarm.extensions.agentos.agentos_router import agent_manager
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentManager
from jiuwenswarm.extensions.agentos.agentos_router import router_client
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    InstanceRecord, RegistryClient, RegistryConfig, instance_service_id,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.stale_cleanup import cleanup_stale_sandboxes


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
    agent = SimpleNamespace(info=info, is_creating=lambda: False)
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=agent), pop_agent=AsyncMock(return_value=agent))
    frontend = SimpleNamespace(delete_sandbox=AsyncMock(side_effect=TimeoutError()))
    registry = SimpleNamespace(unregister_agent=AsyncMock())
    client = AgentOSRouterClient(frontend, registry, manager)
    socket = SimpleNamespace(disconnect=AsyncMock())
    client._ws_clients = {"sandbox": socket}
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
        client._agent_manager.pop_agent.assert_awaited_once()
        client._registry.unregister_agent.assert_awaited_once()
    finally:
        await client._drain_background_tasks()
        parent.finish("success")
        await telemetry.shutdown_gateway_telemetry()


async def test_idle_guard_rejections_create_no_trace_and_preserve_parent(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_gateway_trace("request", root=True)
    client = object.__new__(router_client.AgentOSRouterClient)
    client._sandbox_lifecycles = {}
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
    client._sandbox_lifecycles = {}
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
        assert await cleanup_stale_sandboxes(backend=Frontend(), registry=Registry(), agent_manager=manager) == 4
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
    assert await cleanup_stale_sandboxes(backend=frontend, registry=registry, agent_manager=manager) == 1
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




def spans(setup, name):
    return [span for span in setup.exporter.get_finished_spans() if span.name == name]


def message_trace():
    return telemetry.start_request_trace('r1', 'chat.send', channel='web', connection_id='c1',
                                         trace_context={'trace_id': '11111111-1111-1111-1111-111111111111'})





















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
