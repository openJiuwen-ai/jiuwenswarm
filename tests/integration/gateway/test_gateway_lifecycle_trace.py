# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway lifecycle tracing over real local HTTP and WS connections."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from websockets.legacy.server import serve

from jiuwenswarm.gateway import telemetry
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryClient, RegistryConfig


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



pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_gateway_startup_sweep_tracing_is_configured_before_connect(monkeypatch, enabled):
    from jiuwenswarm.gateway import app_gateway
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common import config
    from jiuwenswarm.extensions.agentos.agentos_router.registry_client import InstanceRecord
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
    from jiuwenswarm.extensions.manager import ExtensionManager

    class StopAfterSweep(Exception):
        pass

    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.setattr(config, "get_config", lambda: {
        "gateway": {"agent_client": {"type": "agentos_router"}},
        "telemetry": {"enabled": enabled, "exporter": "none"},
    })
    exporter = InMemorySpanExporter()
    router = object.__new__(AgentOSRouterClient)
    router._closed = True
    router._sandbox_lifecycles = {}
    router._stale_cleanup_task = None
    router._ensure_idle_reaper_task = Mock()
    router._agent_manager = SimpleNamespace(list_all_agents=AsyncMock(return_value=[]))
    router._registry = SimpleNamespace(
        enabled=True,
        list_instances=AsyncMock(return_value=[InstanceRecord(service_id="service", instance_id="sandbox")]),
        unregister_instance=AsyncMock(return_value={"deleted": True}),
    )

    async def connect_frontend(uri):
        runtime = telemetry._runtime
        assert runtime is not None
        assert (runtime.provider is not None) is enabled
        if enabled:
            runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))

    router._backend = SimpleNamespace(connect=connect_frontend, delete_sandbox=AsyncMock())

    class Client:
        async def connect(self, uri):
            await router.connect(uri)
            await router._stale_cleanup_task
            raise StopAfterSweep

    extension = SimpleNamespace(metadata=SimpleNamespace(id="agentos_router", name="agentos_router"), get_client=Client)
    registry = SimpleNamespace(
        get_agent_server_client_extension=lambda: extension,
        get_third_agent_extension=lambda: None,
    )
    manager = SimpleNamespace(load_all_extensions=AsyncMock(), list_extensions=lambda: [])
    monkeypatch.setattr(ExtensionRegistry, "create_instance", lambda **kwargs: registry)
    monkeypatch.setattr(ExtensionManager, "__new__", lambda cls, **kwargs: manager)

    with pytest.raises(StopAfterSweep):
        await app_gateway._run(
            agent_server_url="", web_host="127.0.0.1", web_port=0, web_path="/ws",
        )
    await telemetry.shutdown_gateway_telemetry()

    assert telemetry._runtime is None
    router._registry.list_instances.assert_awaited_once_with(include_unhealthy=True)
    router._backend.delete_sandbox.assert_awaited_once_with("sandbox")
    assert [span.name for span in exporter.get_finished_spans()] == (["gw.agent.sweep"] if enabled else [])


@pytest.fixture
def prep_tracing(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch, request_id="request", connection_id="connection")
    try:
        yield parent, exporter
    finally:
        parent.complete()
        runtime.provider.shutdown()




@pytest.mark.asyncio
async def test_preparation_headers_reach_real_websocket_handshake(prep_tracing):
    parent, exporter = prep_tracing
    headers = {}

    async def handler(socket, path):
        headers.update(dict(socket.request_headers))
        await socket.send(json.dumps({"type": "event", "event": "connection.ack",
                                      "payload": {"readiness": "AGENT_READY"}}))
        await socket.wait_closed()

    server = await serve(handler, "127.0.0.1", 0)
    client = WebSocketAgentServerClient()
    try:
        with telemetry.bind_request_trace(parent):
            await client.connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                 extra_headers={"Authorization": "test-auth", "X-Trace-Id": "legacy"})
            assert telemetry.current_request_trace() is parent
        span = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.ws.connect")
        _check_trace_headers(headers, span)
        assert headers["authorization"] == "test-auth"
        assert client.server_ready and client.agent_ready
        assert span.parent.span_id == parent.span.get_span_context().span_id
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
